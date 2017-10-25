try: import simplejson as json
except ImportError: import json
#import ijson

from . import Bugzilla, SearchRequest
from ...exceptions import AuthError, RequestError


class IterSearchRequest(SearchRequest):
    def __init__(self, *args, **kw):
        """Construct a search request."""
        super().__init__(*args, **kw)

    def parse(self, data, *args, **kw):
        bugs = ijson.items(data, 'result.bugs.item')
        bugs = (self.bug(service=self, bug=x) for x in bugs)
        return bugs


class BugzillaJsonrpc(Bugzilla):
    """Support Bugzilla's deprecated JSON-RPC interface."""

    #def search(self, *args, **kw):
    #    return IterSearchRequest(self, *args, **kw)

    def __init__(self, **kw):
        self.endpoint = '/jsonrpc.cgi'
        super().__init__(**kw)
        self.session.headers.update({
            'Accept': 'application/json',
            'Content-Type': 'application/json'
        })

    def encode_request(self, method, params=None):
        """Encode the data body for a JSON-RPC request."""
        if self.auth_token is not None:
            # TODO: Is there a better way to determine the difference between
            # tokens and API keys?
            if len(self.auth_token) > 16:
                params['Bugzilla_api_key'] = self.auth_token
            else:
                params['Bugzilla_token'] = self.auth_token

        args = {
            'method': method,
            'params': [params],
            'id': 0,
        }

        return json.dumps(args)

    def parse_response(self, response):
        try:
            data = response.json()
        except json.decoder.JSONDecodeError as e:
            raise RequestError('error decoding response, JSON-RPC interface likely disabled on server')

        if data.get('error') is None:
            return data['result']
        else:
            error = data.get('error')
            if error.get('code') == 32000:
                if self._base.startswith('http:'):
                    # bugzilla strangely returns an error under http but works fine under https
                    raise RequestError('Received error reply, try using an https:// url instead')
                elif 'expired' in error.get('message'):
                    # assume the auth token has expired
                    raise AuthError('auth token expired', expired=True)
            elif error.get('code') == 102:
                raise AuthError('access denied')
            raise RequestError(msg=error.get('message'), code=error.get('code'))

class IterContent(object):
    def __init__(self, file, size=64*1024):
        self.initial = True
        self.chunks = file.iter_content(chunk_size=size)

    def read(self, size=64*1024):
        chunk = next(self.chunks)
        # hacky method of checking the initial chunk for errors
        if self.initial:
            self.initial = False
            if not chunk.startswith(b'{"error":null,'):
                error = json.loads(str(chunk))['error']
                if error['code'] == 102:
                    raise AuthError(msg=error['message'], code=error['code'])
                else:
                    raise RequestError(msg=error['message'], code=error['code'])
        return chunk
