from urllib.parse import parse_qs

from dateutil.parser import parse as parsetime
import lxml.html
import requests
from snakeoil.klass import steal_docs
from snakeoil.sequences import namedtuple

from .objects import BugzillaBug, BugzillaAttachment
from .. import Service
from ...cache import Cache, csv2tuple
from ...exceptions import RequestError, AuthError


class BugzillaError(RequestError):
    """Bugzilla service specific error."""

    def __init__(self, msg, code=None, text=None):
        msg = 'Bugzilla error: ' + msg
        super().__init__(msg, code, text)


class BugzillaCache(Cache):

    def __init__(self, *args, **kw):
        # default to bugzilla-5 open/closed statuses
        defaults = {
            'open_status': ('CONFIRMED', 'IN_PROGRESS', 'UNCONFIRMED'),
            'closed_status': ('RESOLVED', 'VERIFIED'),
        }

        converters = {
            'open_status': csv2tuple,
            'closed_status': csv2tuple,
        }

        super().__init__(defaults=defaults, converters=converters, *args, **kw)


class Bugzilla(Service):
    """Generic bugzilla service support."""

    _cache_cls = BugzillaCache

    item = BugzillaBug
    item_endpoint = '/show_bug.cgi?id='
    attachment = BugzillaAttachment
    attachment_endpoint = '/attachment.cgi?id='

    def __init__(self, max_results=None, *args, **kw):
        # most bugzilla instances default to 10k results per req
        if max_results is None:
            max_results = 10000
        super().__init__(*args, max_results=max_results, **kw)

    @property
    def cache_updates(self):
        """Pull latest data from service for cache update."""
        config_updates = {}
        reqs = []

        # get open/closed status values
        reqs.append(self.FieldsRequest(names=['bug_status']))
        # get available products
        reqs.append(self.ProductsRequest())
        # get server bugzilla version
        reqs.append(self.VersionRequest())

        statuses, products, version = self.send(reqs)

        open_status = []
        closed_status = []
        for status in statuses[0].get('values', []):
            if status.get('name', None) is not None:
                if status.get('is_open', False):
                    open_status.append(status['name'])
                else:
                    closed_status.append(status['name'])
        products = [d['name'] for d in sorted(products, key=lambda x: x['id']) if d['is_active']]
        config_updates['open_status'] = tuple(sorted(open_status))
        config_updates['closed_status'] = tuple(sorted(closed_status))
        config_updates['products'] = tuple(products)
        config_updates['version'] = version

        return config_updates

    @steal_docs(Service)
    def login(self, user, password, restrict_login=False, **kw):
        super().login(user, password, restrict_login=restrict_login)

    @steal_docs(Service)
    def inject_auth(self, request, params):
        if params is None:
            params = {}
        # TODO: Is there a better way to determine the difference between
        # tokens and API keys?
        if len(self.auth) > 16:
            params['Bugzilla_api_key'] = str(self.auth)
        else:
            params['Bugzilla_token'] = str(self.auth)
        return request, params

    class WebSession(Service.WebSession):

        def add_params(self, user, password):
            self.params.update({
                'Bugzilla_login': user,
                'Bugzilla_password': password,
            })

        def login(self):
            # extract auth token to bypass CSRF protection
            # https://bugzilla.mozilla.org/show_bug.cgi?id=713926
            auth_token_name = 'Bugzilla_login_token'
            r = self.session.get(self.service.base)
            doc = lxml.html.fromstring(r.text)
            token = doc.xpath(f'//input[@name="{auth_token_name}"]/@value')[0]
            if not token:
                raise BugzillaError(
                    'failed to extract login token, '
                    f'underlying token name may have changed from {auth_token_name}')

            # login via web form
            self.params[auth_token_name] = token
            r = self.session.post(self.service.base, data=self.params)
            # check that login was successful
            doc = lxml.html.fromstring(r.text)
            login_form = doc.xpath('//input[@name="Bugzilla_login"]')
            if login_form:
                raise AuthError('bad username or password')

            super().login()

    @staticmethod
    def handle_error(code, msg):
        """Handle bugzilla specific errors.

        Bugzilla web service error codes and their descriptions can be found at:
        https://github.com/bugzilla/bugzilla/blob/5.0/Bugzilla/WebService/Constants.pm#L56
        """
        # (-+)32000: fallback error code for unmapped/unknown errors, negative
        # is fatal and positive is transient
        if code == 32000:
            if 'expired' in msg:
                # assume the auth token has expired
                raise AuthError(msg, expired=True)
        # 102: bug access or query denied due to insufficient permissions
        # 410: login required to perform this request
        elif code in (102, 410):
            raise AuthError(msg=msg)
        raise BugzillaError(msg=msg, code=code)

    def _failed_http_response(self, response):
        if response.status_code in (401, 403):
            data = self.parse_response(response)
            raise AuthError(f"authentication failed: {data.get('message', '')}")
        else:
            super()._failed_http_response(response)


class Bugzilla5_0(Bugzilla):
    """Generic bugzilla 5.0 service support."""

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.apikeys = self.ApiKeys(self)
        self.saved_searches = self.SavedSearches(self)

    class ApiKeys(object):
        """Provide access to web service API keys."""

        _ApiKey = namedtuple("_ApiKey", ['key', 'desc', 'used', 'revoked'])

        def __init__(self, service):
            self._service = service
            self._userprefs_url = f"{self._service.base.rstrip('/')}/userprefs.cgi"
            self._doc = None

        @property
        def _keys(self):
            with self._service.web_session() as session:
                # get the apikeys page
                r = session.get(f'{self._userprefs_url}?tab=apikey')
                self._doc = lxml.html.fromstring(r.text)
                # verify API keys table still has the same id
                table = self._doc.xpath('//table[@id="email_prefs"]')
                if not table:
                    raise BiteError('failed to extract API keys table')

                # extract API key info from table
                apikeys = self._doc.xpath('//table[@id="email_prefs"]/tr/td[1]/text()')
                descriptions = self._doc.xpath('//table[@id="email_prefs"]/tr/td[2]/input/@value')
                last_used = self._doc.xpath('//table[@id="email_prefs"]/tr/td[3]//text()')
                revoked = self._doc.xpath('//table[@id="email_prefs"]/tr/td[4]/input')
                revoked = [bool(getattr(x, 'checked', False)) for x in revoked]

                existing_keys = []
                for desc, key, used, revoked in zip(descriptions, apikeys, last_used, revoked):
                    if used != 'never used':
                        used = parsetime(used)
                    existing_keys.append(self._ApiKey(key, desc, used, revoked))

            return existing_keys

        def __iter__(self):
            return iter(self._keys)

        def generate(self, description=None):
            """Generate API keys."""
            with self._service.web_session() as session:
                # check for existing keys with matching descriptions
                try:
                    match = next(k for k in self if k.desc == description)
                    if not self._service.client.confirm(
                            f'{description!r} key already exists, continue?'):
                        return
                except StopIteration:
                    pass

                params = {f'description_{i + 1}': x.desc for i, x in enumerate(self)}
                # add new key fields
                params.update({
                    'new_key': 'on',
                    'new_description': description,
                })

                r = session.post(self._userprefs_url, data=self.add_form_params(params))
                self.verify_changes(r)

        def verify_changes(self, response):
            """Verify that apikey changes worked as expected."""
            doc = lxml.html.fromstring(response.text)
            msg = doc.xpath('//div[@id="message"]/text()')[0].strip()
            if msg != 'The changes to your api keys have been saved.':
                raise BiteError('failed generating apikey', text=msg)

        def add_form_params(self, params):
            """Extract required token data from apikey generation form."""
            apikeys_form = self._doc.xpath('//form[@name="userprefsform"]/input')
            if not apikeys_form:
                # TODO: change to BugzillaError
                raise ValueError('missing form data')
            for x in apikeys_form:
                params[x.name] = x.value
            return params

        def revoke(self, disable=(), enable=()):
            """Revoke and/or unrevoke API keys."""
            with self._service.web_session() as session:
                params = {}
                for i, x in enumerate(self):
                    params[f'description_{i + 1}'] = x.desc
                    if x.revoked:
                        if x.key in enable or x.desc in enable:
                            params[f'revoked_{i + 1}'] = 0
                        else:
                            # have to resubmit already revoked keys
                            params[f'revoked_{i + 1}'] = 1
                    if x.key in disable or x.desc in disable:
                        params[f'revoked_{i + 1}'] = 1

                r = session.post(self._userprefs_url, data=self.add_form_params(params))
                self.verify_changes(r)

    class SavedSearches(object):
        """Provide access to web service saved searches."""

        def __init__(self, service):
            self._service = service
            self._userprefs_url = f"{self._service.base.rstrip('/')}/userprefs.cgi"
            self._doc = None

        @property
        def _searches(self):
            with self._service.web_session() as session:
                # get the saved searches page
                r = session.get(f'{self._userprefs_url}?tab=saved-searches')
                self._doc = lxml.html.fromstring(r.text)
                # verify saved search table still has the same id
                table = self._doc.xpath('//table[@id="saved_search_prefs"]')
                if not table:
                    raise BiteError('failed to extract saved search table')

                # extract saved searches from tables
                names = self._doc.xpath('//table[@id="saved_search_prefs"]/tr/td[1]/text()')
                query_col = self._doc.xpath('//table[@id="saved_search_prefs"]/tr/td[3]')
                queries = []
                for x in query_col:
                    try:
                        queries.append(next(x.iterlinks())[2])
                    except StopIteration:
                        queries.append(None)

                existing_searches = {}
                for name, query in zip(names, queries):
                    if query is None:
                        continue
                    url_params = query.split('?', 1)[1]
                    existing_searches[name] = parse_qs(url_params)

            return existing_searches

        def verify_changes(self, response):
            """Verify that saved search changes worked as expected."""

        def add_form_params(self, params):
            """Extract required token data from saved search form."""

        def save(self, name):
            """Save a given search."""

        def remove(self, name):
            """Remove a given saved search."""

        def __iter__(self):
            return iter(self._searches)

        def __contains__(self, name):
            return name in self._searches

        def get(self, name, default):
            return self._searches.get(name, default)


class Bugzilla5_2(Bugzilla5_0):
    """Generic bugzilla 5.2 service support."""

    # setting auth tokens via headers is supported in >=bugzilla-5.1
    def inject_auth(self, request, params):
        if len(self.auth) > 16:
            self.session.headers['X-BUGZILLA-API-KEY'] = str(self.auth)
        else:
            self.session.headers['X-BUGZILLA-TOKEN'] = str(self.auth)
        self.authenticated = True
        return request, params
