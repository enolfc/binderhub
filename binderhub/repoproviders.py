"""
Classes for Repo providers

Subclass the base class, ``RepoProvider``, to support different version
control services and providers.

Note: When adding a new repo provider, add it to the allowed values for
      repo providers in event-schemas/launch.json.
"""
from datetime import timedelta
import json
import os
import time
import urllib.parse
import re

from prometheus_client import Gauge

from tornado import gen
from tornado.httpclient import AsyncHTTPClient, HTTPError, HTTPRequest
from tornado.httputil import url_concat

from traitlets import Dict, Unicode, Bool, default, List, observe
from traitlets.config import LoggingConfigurable

from .utils import Cache

GITHUB_RATE_LIMIT = Gauge('binderhub_github_rate_limit_remaining', 'GitHub rate limit remaining')
SHA1_PATTERN = re.compile(r'[0-9a-f]{40}')


def tokenize_spec(spec):
    """Tokenize a GitHub-style spec into parts, error if spec invalid."""

    spec_parts = spec.split('/', 2)  # allow ref to contain "/"
    if len(spec_parts) != 3:
        msg = 'Spec is not of the form "user/repo/ref", provided: "{spec}".'.format(spec=spec)
        if len(spec_parts) == 2 and spec_parts[-1] != 'master':
            msg += ' Did you mean "{spec}/master"?'.format(spec=spec)
        raise ValueError(msg)

    return spec_parts


def strip_suffix(text, suffix):
    if text.endswith(suffix):
        text = text[:-(len(suffix))]
    return text


class RepoProvider(LoggingConfigurable):
    """Base class for a repo provider"""
    name = Unicode(
        help="""
        Descriptive human readable name of this repo provider.
        """
    )

    spec = Unicode(
        help="""
        The spec for this builder to parse
        """
    )

    banned_specs = List(
        help="""
        List of specs to blacklist building.

        Should be a list of regexes (not regex objects) that match specs which should be blacklisted
        """,
        config=True
    )

    high_quota_specs = List(
        help="""
        List of specs to assign a higher quota limit.

        Should be a list of regexes (not regex objects) that match specs which should have a higher quota
        """,
        config=True
    )

    spec_config = List(
        help="""
        List of dictionaries that define per-repository configuration.

        Each item in the list is a dictionary with two keys:

            pattern : string
                defines a regex pattern (not a regex object) that matches specs.
            config : dict
                a dictionary of "config_name: config_value" pairs that will be
                applied to any repository that matches `pattern`
        """,
        config=True
    )

    unresolved_ref = Unicode()

    git_credentials = Unicode(
        "",
        help="""
        Credentials (if any) to pass to git when cloning.
        """,
    )

    def is_banned(self):
        """
        Return true if the given spec has been banned
        """
        for banned in self.banned_specs:
            # Ignore case, because most git providers do not
            # count DS-100/textbook as different from ds-100/textbook
            if re.match(banned, self.spec, re.IGNORECASE):
                return True
        return False

    def has_higher_quota(self):
        """
        Return true if the given spec has a higher quota
        """
        for higher_quota in self.high_quota_specs:
            # Ignore case, because most git providers do not
            # count DS-100/textbook as different from ds-100/textbook
            if re.match(higher_quota, self.spec, re.IGNORECASE):
                return True
        return False

    def repo_config(self, settings):
        """
        Return configuration for this repository.
        """
        repo_config = {}

        # Defaults and simple overrides
        if self.has_higher_quota():
            repo_config['quota'] = settings.get('per_repo_quota_higher')
        else:
            repo_config['quota'] = settings.get('per_repo_quota')

        # Spec regex-based configuration
        for item in self.spec_config:
            pattern = item.get('pattern', None)
            config = item.get('config', None)
            if not isinstance(pattern, str):
                raise ValueError(
                    "Spec-pattern configuration expected "
                    "a regex pattern string, not "
                    "type %s" % type(pattern))
            if not isinstance(config, dict):
                raise ValueError(
                    "Spec-pattern configuration expected "
                    "a specification configuration dict, not "
                    "type %s" % type(config))
            # Ignore case, because most git providers do not
            # count DS-100/textbook as different from ds-100/textbook
            if re.match(pattern, self.spec, re.IGNORECASE):
                repo_config.update(config)
        return repo_config

    @gen.coroutine
    def get_resolved_ref(self):
        raise NotImplementedError("Must be overridden in child class")

    def get_repo_url(self):
        """Return the git clone-able repo URL"""
        raise NotImplementedError("Must be overridden in the child class")

    def get_build_slug(self):
        """Return a unique build slug"""
        raise NotImplementedError("Must be overriden in the child class")

    @staticmethod
    def sha1_validate(sha1):
        if not SHA1_PATTERN.match(sha1):
            raise ValueError("resolved_ref is not a valid sha1 hexadecimal hash")


class FakeProvider(RepoProvider):
    """Fake provider for local testing of the UI
    """

    async def get_resolved_ref(self):
        return "1a2b3c4d5e6f"

    def get_repo_url(self):
        return "https://example.com/fake/repo.git"

    def get_build_slug(self):
        return '{user}-{repo}'.format(user='Rick', repo='Morty')


class ZenodoProvider(RepoProvider):
    """Provide contents of a Zenodo record

    Users must provide a spec consisting of the Zenodo DOI.
    """
    name = Unicode("Zenodo")

    @gen.coroutine
    def get_resolved_ref(self):
        client = AsyncHTTPClient()
        req = HTTPRequest("https://doi.org/{}".format(self.spec),
                          user_agent="BinderHub")
        r = yield client.fetch(req)
        self.record_id = r.effective_url.rsplit("/", maxsplit=1)[1]
        return self.record_id

    def get_repo_url(self):
        # While called repo URL, the return value of this function is passed
        # as argument to repo2docker, hence we return the spec as is.
        return self.spec

    def get_build_slug(self):
        return "zenodo-{}".format(self.record_id)


class GitRepoProvider(RepoProvider):
    """Bare bones git repo provider.

    Users must provide a spec of the following form.

    <url-escaped-namespace>/<resolved_ref>

    eg:
    https%3A%2F%2Fgithub.com%2Fjupyterhub%2Fzero-to-jupyterhub-k8s/f7f3ff6d1bf708bdc12e5f10e18b2a90a4795603

    This provider is typically used if you are deploying binderhub yourself and you require access to repositories that
    are not in one of the supported providers.
    """

    name = Unicode("Git")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        url, resolved_ref = self.spec.rsplit('/', 1)
        self.repo = urllib.parse.unquote(url)
        if not resolved_ref:
            raise ValueError("`resolved_ref` must be specified as a query parameter for the basic git provider")
        self.sha1_validate(resolved_ref)
        self.resolved_ref = resolved_ref
        self.unresolved_ref = resolved_ref

    @gen.coroutine
    def get_resolved_ref(self):
        return self.resolved_ref

    def get_repo_url(self):
        return self.repo

    def get_build_slug(self):
        return self.repo


class GitLabRepoProvider(RepoProvider):
    """GitLab provider.

    GitLab allows nested namespaces (eg. root/project/component/repo) thus we need to urlescape the namespace of this
    repo.  Users must provide a spec that matches the following form.

    <url-escaped-namespace>/<unresolved_ref>

    eg:
    group%2Fproject%2Frepo/master
    """

    name = Unicode('GitLab')

    hostname = Unicode('gitlab.com', config=True,
        help="""The host of the GitLab instance

        For personal GitLab servers.
        """
        )

    access_token = Unicode(config=True,
        help="""GitLab OAuth2 access token for authentication with the GitLab API

        For use with client_secret.
        Loaded from GITLAB_ACCESS_TOKEN env by default.
        """
    )
    @default('access_token')
    def _access_token_default(self):
        return os.getenv('GITLAB_ACCESS_TOKEN', '')

    private_token = Unicode(config=True,
        help="""GitLab private token for authentication with the GitLab API

        Loaded from GITLAB_PRIVATE_TOKEN env by default.
        """
    )
    @default('private_token')
    def _private_token_default(self):
        return os.getenv('GITLAB_PRIVATE_TOKEN', '')

    auth = Dict(
        help="""Auth parameters for the GitLab API access

        Populated from access_token, private_token
    """
    )
    @default('auth')
    def _default_auth(self):
        auth = {}
        for key in ('access_token', 'private_token'):
            value = getattr(self, key)
            if value:
                auth[key] = value
        return auth

    @default('git_credentials')
    def _default_git_credentials(self):
        if self.private_token:
            return r'username=binderhub\npassword={token}'.format(token=self.private_token)
        return ""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        quoted_namespace, unresolved_ref = self.spec.split('/', 1)
        self.namespace = urllib.parse.unquote(quoted_namespace)
        self.unresolved_ref = urllib.parse.unquote(unresolved_ref)
        if not self.unresolved_ref:
            raise ValueError("An unresolved ref is required")

    @gen.coroutine
    def get_resolved_ref(self):
        if hasattr(self, 'resolved_ref'):
            return self.resolved_ref

        namespace = urllib.parse.quote(self.namespace, safe='')
        client = AsyncHTTPClient()
        api_url = "https://{hostname}/api/v4/projects/{namespace}/repository/commits/{ref}".format(
            hostname=self.hostname,
            namespace=namespace,
            ref=urllib.parse.quote(self.unresolved_ref, safe=''),
        )
        self.log.debug("Fetching %s", api_url)

        if self.auth:
            # Add auth params. After logging!
            api_url = url_concat(api_url, self.auth)

        try:
            resp = yield client.fetch(api_url, user_agent="BinderHub")
        except HTTPError as e:
            if e.code == 404:
                return None
            else:
                raise

        ref_info = json.loads(resp.body.decode('utf-8'))
        self.resolved_ref = ref_info['id']
        return self.resolved_ref

    def get_build_slug(self):
        # escape the name and replace dashes with something else.
        return '-'.join(p.replace('-', '_-') for p in self.namespace.split('/'))

    def get_repo_url(self):
        return "https://{hostname}/{namespace}.git".format(
            hostname=self.hostname, namespace=self.namespace)


class GitHubRepoProvider(RepoProvider):
    """Repo provider for the GitHub service"""
    name = Unicode('GitHub')

    # shared cache for resolved refs
    cache = Cache(1024)

    hostname = Unicode('github.com',
        config=True,
        help="""The GitHub hostname to use

        Only necessary if not github.com,
        e.g. GitHub Enterprise.
        """)

    client_id = Unicode(config=True,
        help="""GitHub client id for authentication with the GitHub API

        For use with client_secret.
        Loaded from GITHUB_CLIENT_ID env by default.
        """
    )
    @default('client_id')
    def _client_id_default(self):
        return os.getenv('GITHUB_CLIENT_ID', '')

    client_secret = Unicode(config=True,
        help="""GitHub client secret for authentication with the GitHub API

        For use with client_id.
        Loaded from GITHUB_CLIENT_SECRET env by default.
        """
    )
    @default('client_secret')
    def _client_secret_default(self):
        return os.getenv('GITHUB_CLIENT_SECRET', '')

    access_token = Unicode(config=True,
        help="""GitHub access token for authentication with the GitHub API

        Loaded from GITHUB_ACCESS_TOKEN env by default.
        """
    )
    @default('access_token')
    def _access_token_default(self):
        return os.getenv('GITHUB_ACCESS_TOKEN', '')

    auth = Dict(
        help="""Auth parameters for the GitHub API access

        Populated from client_id, client_secret, access_token.
    """
    )
    @default('auth')
    def _default_auth(self):
        auth = {}
        for key in ('client_id', 'client_secret', 'access_token'):
            value = getattr(self, key)
            if value:
                auth[key] = value
        return auth

    @default('git_credentials')
    def _default_git_credentials(self):
        if self.access_token:
            # Based on https://github.com/blog/1270-easier-builds-and-deployments-using-git-over-https-and-oauth
            # If client_id is specified, assuming access_token is personal access token. Otherwise,
            # assume oauth basic token.
            if self.client_id:
                return r'username={client_id}\npassword={token}'.format(
                    client_id=self.client_id, token=self.access_token)
            else:
                return r'username={token}\npassword=x-oauth-basic'.format(token=self.access_token)
        return ""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user, self.repo, self.unresolved_ref = tokenize_spec(self.spec)
        self.repo = strip_suffix(self.repo, ".git")

    def get_repo_url(self):
        return "https://{hostname}/{user}/{repo}".format(
            hostname=self.hostname, user=self.user, repo=self.repo)

    @gen.coroutine
    def github_api_request(self, api_url, etag=None):
        client = AsyncHTTPClient()
        if self.auth:
            # Add auth params. After logging!
            api_url = url_concat(api_url, self.auth)

        headers = {}
        if etag:
            headers['If-None-Match'] = etag
        req = HTTPRequest(api_url, headers=headers, user_agent="BinderHub")

        try:
            resp = yield client.fetch(req)
        except HTTPError as e:
            if e.code == 304:
                resp = e.response
            elif (
                e.code == 403
                and e.response
                and e.response.headers.get('x-ratelimit-remaining') == '0'
            ):
                rate_limit = e.response.headers['x-ratelimit-limit']
                reset_timestamp = int(e.response.headers['x-ratelimit-reset'])
                reset_seconds = int(reset_timestamp - time.time())
                self.log.error(
                    "GitHub Rate limit ({limit}) exceeded. Reset in {delta}.".format(
                        limit=rate_limit,
                        delta=timedelta(seconds=reset_seconds),
                    )
                )
                # round expiry up to nearest 5 minutes
                minutes_until_reset = 5 * (1 + (reset_seconds // 60 // 5))

                raise ValueError("GitHub rate limit exceeded. Try again in %i minutes."
                    % minutes_until_reset
                )
            # Status 422 is returned by the API when we try and resolve a non
            # existent reference
            elif e.code in (404, 422):
                return None
            else:
                raise

        # record and log github rate limit
        remaining = int(resp.headers['x-ratelimit-remaining'])
        rate_limit = int(resp.headers['x-ratelimit-limit'])
        reset_timestamp = int(resp.headers['x-ratelimit-reset'])

        # record with prometheus
        GITHUB_RATE_LIMIT.set(remaining)

        # log at different levels, depending on remaining fraction
        fraction = remaining / rate_limit
        if fraction < 0.2:
            log = self.log.warning
        elif fraction < 0.5:
            log = self.log.info
        else:
            log = self.log.debug

        # str(timedelta) looks like '00:32'
        delta = timedelta(seconds=int(reset_timestamp - time.time()))
        log("GitHub rate limit remaining {remaining}/{limit}. Reset in {delta}.".format(
            remaining=remaining, limit=rate_limit, delta=delta,
        ))
        return resp

    @gen.coroutine
    def get_resolved_ref(self):
        if hasattr(self, 'resolved_ref'):
            return self.resolved_ref

        api_url = "https://api.{hostname}/repos/{user}/{repo}/commits/{ref}".format(
            user=self.user, repo=self.repo, ref=self.unresolved_ref,
            hostname=self.hostname,
        )
        self.log.debug("Fetching %s", api_url)
        cached = self.cache.get(api_url)
        if cached:
            etag = cached['etag']
            self.log.debug("Cache hit for %s: %s", api_url, etag)
        else:
            etag = None

        resp = yield self.github_api_request(api_url, etag=etag)
        if resp is None:
            return None
        if resp.code == 304:
            self.log.info("Using cached ref for %s: %s", api_url, cached['sha'])
            self.resolved_ref = cached['sha']
            # refresh cache entry
            self.cache.move_to_end(api_url)
            return self.resolved_ref
        elif cached:
            self.log.debug("Cache outdated for %s", api_url)

        ref_info = json.loads(resp.body.decode('utf-8'))
        if 'sha' not in ref_info:
            # TODO: Figure out if we should raise an exception instead?
            self.log.warning("No sha for %s in %s", api_url, ref_info)
            self.resolved_ref = None
            return None
        # store resolved ref and cache for later
        self.resolved_ref = ref_info['sha']
        self.cache.set(
            api_url,
            {
                'etag': resp.headers.get('ETag'),
                'sha': self.resolved_ref,
            },
        )
        return self.resolved_ref

    def get_build_slug(self):
        return '{user}-{repo}'.format(user=self.user, repo=self.repo)


class GistRepoProvider(GitHubRepoProvider):
    """GitHub gist provider.

    Users must provide a spec that matches the following form (similar to github)

    [https://gist.github.com/]<username>/<gist-id>[/<ref>]

    The ref is optional, valid values are
        - a full sha1 of a ref in the history
        - master
    If master or no ref is specified the latest revision will be used.
    """

    name = Unicode('Gist')

    allow_secret_gist = Bool(
        default_value=False,
        config=True,
        help="Flag for allowing usages of secret Gists.  The default behavior is to disallow secret gists.",
    )

    def __init__(self, *args, **kwargs):
        # We dont need to initialize entirely the same as github
        super(RepoProvider, self).__init__(*args, **kwargs)
        parts = self.spec.split('/')
        self.user, self.gist_id, *_ = parts
        if len(parts) > 2:
            self.unresolved_ref = parts[2]
        else:
            self.unresolved_ref = ''

    def get_repo_url(self):
        return f'https://gist.github.com/{self.user}/{self.gist_id}.git'

    @gen.coroutine
    def get_resolved_ref(self):
        if hasattr(self, 'resolved_ref'):
            return self.resolved_ref

        api_url = f"https://api.github.com/gists/{self.gist_id}"
        self.log.debug("Fetching %s", api_url)

        resp = yield self.github_api_request(api_url)
        if resp is None:
            return None

        ref_info = json.loads(resp.body.decode('utf-8'))

        if (not self.allow_secret_gist) and (not ref_info['public']):
            raise ValueError("You seem to want to use a secret Gist, but do not have permission to do so. "
                             "To enable secret Gist support, set (or have an administrator set) "
                             "'GistRepoProvider.allow_secret_gist = True'")

        all_versions = [e['version'] for e in ref_info['history']]
        if (len(self.unresolved_ref) == 0) or (self.unresolved_ref == 'master'):
            self.resolved_ref = all_versions[0]
        else:
            if self.unresolved_ref not in all_versions:
                return None
            else:
                self.resolved_ref = self.unresolved_ref

        return self.resolved_ref

    def get_build_slug(self):
        return self.gist_id
