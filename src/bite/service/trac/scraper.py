"""Web scraper for Trac without RPC support."""

from urllib.parse import urlparse, parse_qs

from dateutil.parser import parse as parsetime
from snakeoil.klass import aliased, alias

from . import TracTicket, TracAttachment
from .._html import HTML
from .._rest import REST, RESTRequest
from .._reqs import Request, ParseRequest, req_cmd


class TracScraper(HTML, REST):
    """Service supporting the Trac-based ticket trackers."""

    _service = 'trac-scraper'

    item = TracTicket
    item_endpoint = '/ticket/{id}'
    attachment = TracAttachment

    def __init__(self, max_results=None, **kw):
        # unsure if there is a sane upper limit on the max items per page, but we'll use 250
        if max_results is None:
            max_results = 250
        super().__init__(max_results=max_results, **kw)


@req_cmd(TracScraper, cmd='search')
class _SearchRequest(ParseRequest, RESTRequest):
    """Construct a search request.

    Query docs:
        https://trac.edgewall.org/wiki/TracQuery
    """

    # map from standardized kwargs name to expected service parameter name
    _params_map = {
        'created': 'time',
        'modified': 'changetime',
        'sort': 'order',
    }

    def __init__(self, **kw):
        super().__init__(endpoint='/query', **kw)

    def parse(self, data):
        """Parsing function for the raw HTML pages."""
        try:
            table = data.xpath('//table[@class="listing tickets"]')[0]
        except IndexError:
            raise BiteError('no issues exist')
        for row in table.xpath('./tbody/tr'):
            cols = row.xpath('./td')
            # no issues exist
            if len(cols) <= 1:
                break
            d = {}
            for c in cols:
                k = c.get('class')
                try:
                    a = c.xpath('./a')[0]
                    if k.endswith('time'):
                        v = parsetime(
                            parse_qs(urlparse(next(a.iterlinks())[2])[4])['from'][0])
                    else:
                        v = a.text
                except IndexError:
                    v = c.text.strip()
                # strip number symbol from IDs if it exists
                if k == 'id' and v[0] == '#':
                    v = v[1:]
                d[c.get('class')] = v
            yield self.service.item(self.service, get_desc=False, **d)

    @aliased
    class ParamParser(ParseRequest.ParamParser):

        # Map of allowed sorting input values to service parameters.
        _sorting_map = {
            'assignee': 'owner',
            'id': 'id',
            'created': 'created',
            'modified': 'modified',
            'status': 'status',
            'description': 'description',
            'creator': 'reporter',
            'milestone': 'milestone',
            'component': 'component',
            'summary': 'summary',
            'priority': 'priority',
            'keywords': 'keywords',
            'version': 'version',
            'platform': 'platform',
            'difficulty': 'difficulty',
            'type': 'type',
            'wip': 'wip',
            'severity': 'severity',
        }

        def _finalize(self, **kw):
            # default to sorting ascending by ID
            sort = self.params.pop('sort', {'order': 'id'})

            if not self.params:
                raise BiteError('no supported search terms or options specified')

            # disable results paging
            self.params['max'] = self.service.max_results

            # default to sorting ascending by ID
            self.params.update(sort)

            # default to returning only open tickets
            if 'status' not in self.params:
                self.params['status'] = '!closed'

        def terms(self, k, v):
            or_queries = []
            display_terms = []
            for term in v:
                or_terms = [x.replace('"', '\\"') for x in term.split(',')]
                or_display_terms = [f'"{x}"' for x in or_terms]
                if len(or_terms) > 1:
                    or_queries.append('|'.join(or_terms))
                    display_terms.append(f"({' OR '.join(or_display_terms)})")
                else:
                    or_queries.append(or_terms[0])
                    display_terms.append(or_display_terms[0])
            # space-separated AND queries are only supported in 1.2.1 onwards
            # https://trac.edgewall.org/ticket/10152
            self.params['summary'] = f"~{' '.join(or_queries)}"
            self.options.append(f"Summary: {' AND '.join(display_terms)}")

        @alias('modified')
        def created(self, k, v):
            self.params[k] = f'{v.isoformat()}..'
            self.options.append(f'{k.capitalize()}: {v} (since {v.isoformat()})')

        def sort(self, k, v):
            if v[0] == '-':
                key = v[1:]
                desc = 1
            else:
                key = v
                desc = 0
            try:
                order_var = self._sorting_map[key]
            except KeyError:
                choices = ', '.join(sorted(self._sorting_map.keys()))
                raise BiteError(
                    f'unable to sort by: {key!r} (available choices: {choices}')
            d = {'order': order_var}
            if desc:
                d['desc'] = desc
            self.params[k] = d
            self.options.append(f"Sort order: {v}")

        @alias('reporter')
        def owner(self, k, v):
            self.params[k] = '|'.join(v)
            self.options.append(f"{self.service.item.attributes[k]}: {', '.join(v)}")
