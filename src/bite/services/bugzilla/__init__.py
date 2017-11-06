import base64
import codecs
import datetime
from itertools import groupby
import os
import re
import string

from dateutil.parser import parse as dateparse

from .. import Service, Request, NullRequest, command, request
from ... import const, magic, utc
from ...cache import Cache, csv2tuple
from ...exceptions import RequestError, BiteError
from ...objects import Item, Change, Comment, Attachment, decompress


class BugzillaError(RequestError):

    def __init__(self, msg, code=None, text=None):
        msg = 'Bugzilla error: ' + msg
        super().__init__(msg, code, text)

def parsetime(time):
    if not isinstance(time, datetime.datetime):
        return dateparse(str(time))
    else:
        return time.replace(tzinfo=utc.utc)


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

    def __init__(self, restrict_login=False, **kw):
        self.restrict_login = restrict_login
        super().__init__(cache_cls=BugzillaCache, **kw)

        self.item = BugzillaBug
        self.attachment = BugzillaAttachment

        # TODO: temporary compat
        self.attributes = self.item.attributes
        self.attribute_aliases = self.item.attribute_aliases

    @property
    def cache_updates(self):
        """Pull latest data from service for cache update."""
        config_updates = {}
        reqs = []

        # get open/closed status values
        reqs.append(self.FieldsRequest(names=['bug_status']))
        # get server bugzilla version
        reqs.append(self.VersionRequest())

        statuses, version = self.send(reqs)

        open_status = []
        closed_status = []
        for status in statuses[0].get('values', []):
            if status.get('name', None) is not None:
                if status.get('is_open', False):
                    open_status.append(status['name'])
                else:
                    closed_status.append(status['name'])
        config_updates['open_status'] = tuple(sorted(open_status))
        config_updates['closed_status'] = tuple(sorted(closed_status))
        config_updates['version'] = version

        return config_updates

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

    def query(self, method, params=None):
        """Query bugzilla for various data."""
        req = self.create_request(method=method, params=params)
        data = self.send(req)
        return data


@request(Bugzilla)
class _LoginRequest(Request):
    def __init__(self, service, user, password, restrict_login=None):
        """Log in as a user and get an auth token."""
        if restrict_login is None:
            restrict_login = service.restrict_login

        params = {
            'login': user,
            'password': password,
            'restrict_login': restrict_login,
        }
        super().__init__(service=service, method='User.login', params=params)

    def parse(self, data):
        return next(data)['token']


@command('users', Bugzilla)
@request(Bugzilla)
class _UsersRequest(Request):
    def __init__(self, service, ids=None, names=None, match=None):
        """Query bugzilla for user data."""
        if not any((ids, names, match)):
            raise ValueError('No user ID(s), name(s), or match(es) specified')

        params = {}

        if ids is not None:
            params['ids'] = ids
        if names is not None:
            params['names'] = names
        if match is not None:
            params['match'] = match

        super().__init__(service=service, method='User.get', params=params)

    def parse(self, data):
        return next(data)['users']


@command('products', Bugzilla)
@request(Bugzilla)
class _ProductsRequest(Request):
    def __init__(self, service, ids=None, names=None, match=None):
        """Query bugzilla for product data."""
        if not any((ids, names)):
            raise ValueError('No user ID(s) or name(s) specified')

        params = {}

        if ids is not None:
            params['ids'] = ids
        if names is not None:
            params['names'] = names

        super().__init__(service=service, method='Product.get', params=params)

    def parse(self, data):
        return next(data)['products']


@command('extensions', Bugzilla)
@request(Bugzilla)
class _ExtensionsRequest(Request):
    def __init__(self, service):
        """Construct an extensions request."""
        super().__init__(service=service, method='Bugzilla.extensions')

    def parse(self, data):
        return next(data)['extensions']


@command('version', Bugzilla)
@request(Bugzilla)
class _VersionRequest(Request):
    def __init__(self, service):
        """Construct a version request."""
        super().__init__(service=service, method='Bugzilla.version')

    def parse(self, data):
        return next(data)['version']


@command('get', Bugzilla)
@request(Bugzilla)
class _GetRequest(Request):
    def __init__(self, service, ids, fields=None, get_comments=False,
                 get_attachments=False, get_history=False, **kw):
        """Construct a get request."""
        if not ids:
            raise ValueError('No bug ID(s) specified')

        params = {}
        params['permissive'] = True
        params['ids'] = ids
        if fields is not None:
            params['include_fields'] = fields

        reqs = []
        for call in ('attachments', 'comments', 'history'):
            if locals()['get_' + call]:
                reqs.append(getattr(service, call.capitalize() + 'Request')(ids=ids))
            else:
                reqs.append(NullRequest())

        super().__init__(service=service, method='Bug.get', params=params, reqs=reqs)

    def parse(self, data):
        data, attachments, comments, history = data
        bugs = next(data)['bugs']
        return (self.service.item(self.service, bug, next(comments), next(attachments), next(history)) for bug in bugs)


@command('create', Bugzilla)
@request(Bugzilla)
class _CreateRequest(Request):
    def __init__(self, service, product, component, version, summary, description=None, op_sys=None,
               platform=None, priority=None, severity=None, alias=None, assigned_to=None,
               cc=None, target_milestone=None, groups=None, status=None, **kw):
        """Create a new bug given a list of parameters

        :returns: ID of the newly created bug
        :rtype: int
        """
        params = {}
        params['product'] = product
        params['component'] = component
        params['version'] = version
        params['summary'] = summary
        if description is not None:
            params['description'] = description
        if op_sys is not None:
            params['op_sys'] = op_sys
        if platform is not None:
            params['platform'] = platform
        if priority is not None:
            params['priority'] = priority
        if severity is not None:
            params['severity'] = severity
        if alias is not None:
            params['alias'] = alias
        if assigned_to is not None:
            params['assigned_to'] = assigned_to
        if cc is not None:
            params['cc'] = cc
        if target_milestone is not None:
            params['target_milestone'] = target_milestone
        if groups is not None:
            params['groups'] = groups
        if status is not None:
            params['status'] = status

        super().__init__(service=service, method='Bug.create', params=params)

    def parse(self, data, *args, **kw):
        return next(data)['id']


@command('search', Bugzilla)
@request(Bugzilla)
class _SearchRequest(Request):
    def __init__(self, service, *args, **kw):
        """Construct a search request."""
        params = {}
        options_log = []
        for k, v in ((k, v) for (k, v) in kw.items() if v):
            if k in BugzillaBug.attributes:
                if k in ['creation_time', 'last_change_time']:
                    params[k] = v[1]
                    options_log.append('{}: {} (since {} UTC)'.format(BugzillaBug.attributes[k], v[0], parsetime(v[1])))
                elif k in ['assigned_to', 'creator']:
                    params[k] = list(map(service._resuffix, v))
                    options_log.append('{}: {}'.format(BugzillaBug.attributes[k], ', '.join(map(str, v))))
                elif k == 'status':
                    status_alias = []
                    status_map = {
                        'open': service.cache['open_status'],
                        'closed': service.cache['closed_status'],
                        'all': service.cache['open_status'] + service.cache['closed_status'],
                    }
                    for status in v:
                        if status_map.get(status.lower(), False):
                            status_alias.append(status)
                            params.setdefault(k, []).extend(status_map[status.lower()])
                        else:
                            params.setdefault(k, []).append(status)
                    if status_alias:
                        options_log.append('{}: {} ({})'.format(BugzillaBug.attributes[k], ', '.join(status_alias), ', '.join(params[k])))
                    else:
                        options_log.append('{}: {}'.format(BugzillaBug.attributes[k], ', '.join(params[k])))
                else:
                    params[k] = v
                    options_log.append('{}: {}'.format(BugzillaBug.attributes[k], ', '.join(map(str, v))))
            else:
                if k == 'terms':
                    params['summary'] = v
                    options_log.append('{}: {}'.format('Summary', ', '.join(map(str, v))))
                elif k == 'commenter':
                    # XXX: probably fragile since it uses custom search URL params
                    # only works with >=bugzilla-5, previous versions return invalid parameter errors
                    for i, val in enumerate(v):
                        i = str(i + 1)
                        params['f' + i] = 'commenter'
                        params['o' + i] = 'substring'
                        params['v' + i] = val
                    options_log.append('{}: {}'.format('Commenter', ', '.join(map(str, v))))
                elif k in ['limit', 'offset', 'votes']:
                    params[k] = v

        if not params:
            raise BiteError('no supported search terms or options specified')

        # only return open bugs by default
        if not 'status' in params:
            params['status'] = service.cache['open_status']

        if not 'fields' in kw or kw['fields'] is None:
            fields = ['id', 'assigned_to', 'summary']
        else:
            fields = kw['fields']
            unknown_fields = set(fields).difference(BugzillaBug.attributes.keys())
            if unknown_fields:
                raise BiteError('unknown fields: {}'.format(', '.join(unknown_fields)))
            options_log.append('{}: {}'.format('Fields', ' '.join(fields)))

        params['include_fields'] = fields

        super().__init__(service=service, method='Bug.search', params=params)
        self.fields = fields
        self.options = options_log

    def parse(self, data, *args, **kw):
        bugs = next(data)['bugs']
        return (self.service.item(service=self.service, bug=bug) for bug in bugs)


@command('comments', Bugzilla)
@request(Bugzilla)
class _CommentsRequest(Request):
    def __init__(self, service, ids=None, comment_ids=None, created=None, fields=None, *args, **kw):
        """Construct a comments request."""
        if ids is None and comment_ids is None:
            raise ValueError('No {} or comment ID(s) specified'.format(self.service.item_name))

        params = {}

        if ids is not None:
            params['ids'] = ids
        if comment_ids is not None:
            params['comment_ids'] = comment_ids
        if created is not None:
            params['new_since'] = created
        if fields is not None:
            params['include_fields'] = fields

        self.ids = ids

        super().__init__(service=service, method='Bug.comments', params=params)
        # TODO: this
        self.options = ['REPLACE ME']

    def parse(self, data, *args, **kw):
        bugs = next(data)['bugs']
        for i in self.ids:
            yield [BugzillaComment(comment=comment, id=i, count=j) for j, comment in enumerate(bugs[str(i)]['comments'])]


class ChangesRequest(Request):
    pass


@command('modify', Bugzilla)
@request(Bugzilla)
class _ModifyRequest(Request):
    def __init__(self, service, ids, *args, **kw):
        """Construct a modify request."""
        options_log = []
        params = {}
        for k, v in ((k, v) for (k, v) in kw.items() if v):
            if k in BugzillaBug.attributes:
                if k == 'assigned_to':
                    v = self.service._resuffix(v)
                params[k] = v
                options_log.append('{:<10}: {}'.format(BugzillaBug.attributes[k], v))
            elif '-' in k:
                keys = k.split('-')
                if len(keys) != 2:
                    raise RuntimeError('Argument parsing error')
                else:
                    if keys[0] == 'cc':
                        v = list(map(self.service._resuffix, v))
                    if k == 'comment-body':
                        v = codecs.getdecoder('unicode_escape')(v)[0]

                    if keys[0] not in kw:
                        params[keys[0]] = {}

                    params[keys[0]][keys[1]] = v

                    if keys[1] in ['add', 'remove', 'set']:
                        options_log.append((keys[0], keys[1], v))
                    elif keys[0] == 'comment':
                        pass
                    else:
                        try:
                            options_log.append('{:<10}: {}'.format(BugzillaBug.attributes[keys[0]], v))
                        except KeyError:
                            options_log.append('{:<10}: {}'.format(keys[0].capitalize(), v))
            else:
                if k == 'fixed':
                    params['status'] = 'RESOLVED'
                    params['resolution'] = 'FIXED'
                    options_log.append('Status    : RESOLVED')
                    options_log.append('Resolution: FIXED')
                elif k == 'invalid':
                    params['status'] = 'RESOLVED'
                    params['resolution'] = 'INVALID'
                    options_log.append('Status    : RESOLVED')
                    options_log.append('Resolution: INVALID')

        merge_targets = ((i, x) for i, x in enumerate(options_log) if isinstance(x, tuple))
        merge_targets = sorted(merge_targets, key=lambda x: x[1][0])
        old_indices = [i for (i, x) in merge_targets]
        for key, group in groupby(merge_targets, lambda x: x[1][0]):
            value = []
            for (_, (key, action, values)) in group:
                if action == 'add':
                    value.extend(['+' + str(x) for x in values])
                elif action == 'remove':
                    value.extend(['-' + str(x) for x in values])
                elif action == 'set':
                    value = values
                    break
            try:
                options_log.append('{:<10}: {}'.format(BugzillaBug.attributes[key], ', '.join(value)))
            except KeyError:
                options_log.append('{:<10}: {}'.format(key.capitalize(), ', '.join(value)))

        # remove old entries
        options_log = [x for i, x in enumerate(options_log) if i not in old_indices]

        if not params:
            raise ValueError('No changes specified')

        if options_log:
            prefix = '--- Modifying fields '
            options_log.insert(0, prefix + '-' * (const.COLUMNS - len(prefix)))

        if 'comment' in params:
            prefix = '--- Adding comment '
            options_log.append(prefix + '-' * (const.COLUMNS - len(prefix)))
            options_log.append(params['comment']['body'])

        options_log.append('-' * const.COLUMNS)

        if not ids:
            raise ValueError('No bug ID(s) specified')
        params['ids'] = ids

        super().__init__(service=service, method='Bug.update', params=params)
        self.options = options_log

    def parse(self, data, *args, **kw):
        return next(data)['bugs']


@command('attach', Bugzilla)
@request(Bugzilla)
class _AttachRequest(Request):
    def __init__(self, ids, data=None, filepath=None, filename=None, mimetype=None,
                 is_patch=False, is_private=False, comment=None, summary=None, **kw):
        """Add an attachment to a bug

        :param ids: The ids or aliases of bugs that you want to add the attachment to.
        :type ids: list of ints and/or strings
        :param data: Raw attachment data
        :type data: binary data
        :param filepath: Path to the file.
        :type filepath: string
        :param filename: The file name that will be displayed in the UI for the attachment.
        :type filename: string
        :param mimetype: The MIME type of the attachment, like text/plain or image/png.
        :type mimetype: string
        :param comment: A comment to add along with the attachment.
        :type comment: string
        :param summary: A short string describing the attachment.
        :type summary: string
        :param is_patch: True if Bugzilla should treat this attachment as a patch.
            If specified, a content_type doesn't need to be specified as it is forced to text/plain.
            Defaults to false if unspecified.
        :type is_patch: boolean
        :param is_private: True if the attachment should be private, False if public.
            Defaults to false if unspecified.
        :type is_private: boolean

        :raises ValueError: if no bug IDs are specified
        :raises ValueError: if data or filepath arguments aren't specified
        :raises ValueError: if data isn't defined and filepath points to a nonexistent file
        :raises ValueError: if filepath isn't defined and summary or filename isn't specified

        :returns: attachment IDs created
        :rtype: list of attachment IDs
        """
        if not ids:
            raise ValueError('No bug ID(s) or aliases specified')

        params = {'ids': ids}

        if data is not None:
            params['data'] = base64.b64encode(data)
        else:
            if filepath is None:
                raise ValueError('Either data or a filepath must be passed as an argument')
            else:
                if not os.path.exists(filepath):
                    raise ValueError('File not found: {}'.format(filepath))
                else:
                    with open(filepath, 'rb') as f:
                        params['data'] = base64.b64encode(f.read())

        if filename is None:
            if filepath is not None:
                filename = os.path.basename(filepath)
            else:
                raise ValueError('A valid filename must be specified')

        if mimetype is None and not is_patch:
            if data is not None:
                mimetype = magic.from_buffer(data, mime=True)
            else:
                mimetype = magic.from_file(filepath, mime=True)

        if summary is None:
            if filepath is not None:
                summary = filename
            else:
                raise ValueError('A valid summary must be specified')

        params['file_name'] = filename
        params['summary'] = summary
        if not is_patch:
            params['content_type'] = mimetype
        params['comment'] = comment
        params['is_patch'] = is_patch

        super().__init__(service=service, method='Bug.add_attachment', params=params)

    def parse(self, data, *args, **kw):
        return next(data)['attachments']


@command('attachments', Bugzilla)
@request(Bugzilla)
class _AttachmentsRequest(Request):
    def __init__(self, service, ids=None, attachment_ids=None, fields=None,
                 get_data=False, *args, **kw):
        """Construct a attachments request."""
        if ids is None and attachment_ids is None:
            raise ValueError('No {} or attachment ID(s) specified'.format(self.service.item_name))

        params = {}

        if ids is not None:
            params['ids'] = ids
        if attachment_ids is not None:
            params['attachment_ids'] = attachment_ids
        if fields is not None:
            params['include_fields'] = fields
        # attachment data doesn't get pulled by default
        if not get_data:
            params['exclude_fields'] = ['data']
        super().__init__(service=service, method='Bug.attachments', params=params)

        # TODO: this
        self.options = ['REPLACE ME']

        self.ids = ids
        self.attachment_ids = attachment_ids

    def parse(self, data, *args, **kw):
        data = next(data)
        if self.ids:
            bugs = data['bugs']
            for i in self.ids:
                yield [self.service.attachment(**attachment) for attachment in bugs[str(i)]]

        if self.attachment_ids:
            attachments = data['attachments']
            files = []
            try:
                for i in self.attachment_ids:
                    files.append(self.service.attachment(**attachments[str(i)]))
            except KeyError:
                raise BiteError('invalid attachment ID: {}'.format(i))
            yield files


@command('history', Bugzilla)
@request(Bugzilla)
class _HistoryRequest(Request):
    def __init__(self, service, ids, *args, **kw):
        if not ids:
            raise ValueError('No bug ID(s) specified')
        params = {'ids': ids}
        super().__init__(service=service, method='Bug.history', params=params)

        # TODO: this
        self.options = ['REPLACE ME']

    def parse(self, data, *args, **kw):
        bugs = next(data)['bugs']
        for b in bugs:
            yield [BugzillaEvent(change=x, id=b['id'], alias=b['alias'], count=i) for i, x in enumerate(b['history'], start=1)]


@command('fields', Bugzilla)
@request(Bugzilla)
class _FieldsRequest(Request):
    def __init__(self, service, ids=None, names=None, *args, **kw):
        """Get information about valid bug fields.

        :param ids: fields IDs
        :type ids: list of ints
        :param names: field names
        :type names: list of strings

        """
        params = {}
        options_log = []

        if ids is None and names is None:
            options_log.append('all non-obsolete fields')

        if ids is not None:
            params['ids'] = ids
            options_log.append('IDs: {}'.format(', '.join(ids)))
        if names is not None:
            params['names'] = names
            options_log.append('Field names: {}'.format(', '.join(names)))

        super().__init__(service=service, method='Bug.fields', params=params)
        self.options = options_log

    def parse(self, data, *args, **kw):
        return next(data)['fields']


class BugzillaBug(Item):

    attributes = {
        'actual_time': 'Actual time',
        'alias': 'Alias',
        'assigned_to': 'Assignee',
        'attachments': 'Attachments',
        'blocks': 'Blocks',
        'cc': 'CC',
        'classification': 'Classification',
        'comments': 'Comments',
        'component': 'Component',
        'creation_time': 'Created',
        'creator': 'Reporter',
        'deadline': 'Deadline',
        'depends_on': 'Depends',
        'dupe_of': 'Duplicate of',
        'estimated_time': 'Estimated time',
        'flags': 'Flags',
        'groups': 'Groups',
        'history': 'History',
        'id': 'ID',
        'is_cc_accessible': 'Is CC Accessible',
        'is_confirmed': 'Confirmed',
        'is_creator_accessible': 'Is Creator Accessible',
        'keywords': 'Keywords',
        'last_change_time': 'Modified',
        'op_sys': 'Operating System',
        'platform': 'Platform',
        'priority': 'Priority',
        'product': 'Product',
        'qa_contact': 'QA Contact',
        'ref': 'Reference',
        'remaining_time': 'Remaining time',
        'resolution': 'Resolution',
        'see_also': 'See also',
        'severity': 'Severity',
        'status': 'Status',
        'summary': 'Title',
        'target_milestone': 'Target milestone',
        'url': 'URL',
        'version': 'Version',
        'whiteboard': 'Whiteboard',
    }

    attribute_aliases = {
        'owner': 'assigned_to',
        'modified': 'last_change_time',
        'created': 'creation_time',
        'depends': 'depends_on',
        'title': 'summary'
    }

    endpoint = '/show_bug.cgi?id='
    type = 'bug'

    def __init__(self, service, bug, comments=None, attachments=None, history=None, **kw):
        self.service = service
        for k, v in bug.items():
            if not v or v == '---':
                # skip empty lists and blank fields
                continue
            elif v == 'flags':
                self.flags = [flag['name'] for flag in bug['flags']]
            elif k in ['creation_time', 'last_change_time']:
                setattr(self, k, parsetime(v))
            else:
                if isinstance(v, str) and re.match(r'^\d\d\d\d-\d\d-\d\dT\d\d:\d\d:\d\dZ$', v):
                    setattr(self, k, parsetime(v))
                else:
                    setattr(self, k, v)

        if attachments:
            self.attachments = attachments
        if comments:
            self.comments = comments
        if history:
            self.history = history

    def __str__(self):
        lines = []
        print_fields = [
            ('summary', 'Title'),
            ('alias', 'Alias'),
            ('assigned_to', 'Assignee'),
            ('creator', 'Reporter'),
            ('qa_contact', 'QA Contact'),
            ('creation_time', 'Reported'),
            ('last_change_time', 'Updated'),
            ('status', 'Status'),
            ('resolution', 'Resolution'),
            ('dupe_of', 'Duplicate'),
            ('whiteboard', 'Whiteboard'),
            ('severity', 'Severity'),
            ('priority', 'Priority'),
            ('classification', 'Class'),
            ('product', 'Product'),
            ('component', 'Component'),
            ('platform', 'Platform'),
            ('op_sys', 'OS'),
            ('keywords', 'Keywords'),
            ('target_milestone', 'Target'),
            ('version', 'Version'),
            ('url', 'URL'),
            ('ref', 'Reference'),
            ('see_also', 'See also'),
            ('cc', 'CC'),
            ('id', 'ID'),
            ('blocks', 'Blocks'),
            ('depends_on', 'Depends'),
            ('flags', 'Flags'),
            ('groups', 'Groups'),
            ('estimated_time', 'Estimated'),
            ('deadline', 'Deadline'),
            ('actual_time', 'Actual'),
            ('remaining_time', 'Remaining'),
            #('is_cc_accessible', 'Is CC Accessible'),
            #('is_confirmed', 'Confirmed'),
            #('is_creator_accessible', 'Is Creator Accessible'),
            ('history', 'Changes'),
            ('comments', 'Comments'),
            ('attachments', 'Attachments'),
        ]

        for field, title in print_fields:
            value = getattr(self, field)
            if value is None:
                continue

            if field in ['history', 'comments', 'attachments']:
                value = len(value)

            # Initial comment is the bug description
            if field == 'comments': value -= 1

            if isinstance(value, list):
                value = ', '.join(map(str, value))

            lines.append('{:<12}: {}'.format(title, value))

        custom_fields = ((k, v) for (k, v) in vars(self).items()
                         if re.match(r'^cf_\w+$', k))
        for k, v in custom_fields:
            if isinstance(v, list):
                value = ', '.join(v)
            else:
                value = v
            title = string.capwords(k[3:], '_')
            title = title.replace('_', ' ')
            lines.append('{:<12}: {}'.format(title, value))

        return '\n'.join(lines)

    def __getattribute__(self, name):
        value = object.__getattribute__(self, name)
        if name == 'cc' and isinstance(value, list):
            return list(map(self.service._desuffix, value))
        elif isinstance(value, str):
            return self.service._desuffix(value)
        else:
            return value

    def __getattr__(self, name):
        if name in self.attributes.keys():
            return None
        else:
            raise AttributeError

class BugzillaComment(Comment):
    def __init__(self, comment, id, count, rest=False, **kw):
        self.comment_id = comment['id']

        if rest:
            if 'real_name' in comment['creator'] and comment['creator']['real_name'] != '':
                creator = '{} ({})'.format(comment['creator']['real_name'], comment['creator']['name'])
            else:
                creator = comment['creator']['name']
        else:
            creator = comment['creator']

        date = parsetime(comment['creation_time'])
        count = comment['count']

        if comment['text'] is None:
            text = None
        else:
            text = comment['text'].strip()

        changes = {}
        if 'attachment_id' in comment:
            changes['attachment_id'] = comment['attachment_id']

        super().__init__(
            id=id, creator=creator, date=date,
            count=count, changes=changes, text=text)

class BugzillaEvent(Change):
    def __init__(self, change, id, alias=None, count=None, rest=False, **kw):
        self.alias = alias
        if rest:
            creator = change['changer']['name']
            date = parsetime(change['change_time'])
        else:
            creator = change['who']
            date = parsetime(change['when'])
        changes = change['changes']
        super().__init__(
            creator=creator, date=date, id=id,
            changes=changes, count=count)

    def __str__(self):
        change_fields = {
            'attachments.isobsolete': 'Obsolete attachment',
            'attachments.ispatch': 'Patch attachment',
            'attachments.description': 'Attachment description',
            'attachments.filename': 'Attachment filename',
            'attachments.mimetype': 'Attachment mimetype',
            'blocked': 'Blocks',
            'bug_file_loc': 'URL',
            'bug_group': 'Group',
            'bug_severity': 'Severity',
            'bug_status': 'Status',
            'cclist_accessible': 'CCs accessible',
            'dependson': 'Depends',
            'everconfirmed': 'Confirmed',
            'flag': 'Flag',
            'flagtypes.name': 'Flag type name',
            'rep_platform': 'Platform',
            'reporter_accessible': 'Reporter accessible',
            'short_desc': 'Title',
            'status_whiteboard': 'Whiteboard',
        }
        change_fields.update(BugzillaBug.attributes)

        lines = ['Change #{} by {}, {}'.format(self.count, self.creator, self.date)]
        lines.append('-' * const.COLUMNS)
        for change in self.changes:
            try:
                field = change_fields[change['field_name']]
            except KeyError:
                field = change['field_name']
                if re.match(r'^cf_\w+$', field):
                    field = string.capwords(field[3:], '_')
                    field = field.replace('_', ' ')

            if change['field_name'] == 'attachments.isobsolete':
                lines.append('{}: {}'.format(field, change['attachment_id']))
            else:
                if change['removed'] and change['added']:
                    changes = '{} -> {}'.format(change['removed'], change['added'])
                elif change['removed']:
                    changes = ', '.join(['-' + c for c in change['removed'].split(', ')])
                elif change['added']:
                    changes = ', '.join(['+' + c for c in change['added'].split(', ')])
                lines.append('{}: {}'.format(field, changes))

        return '\n'.join(lines)

class BugzillaAttachment(Attachment):

    endpoint = '/attachment.cgi?id='

    def __init__(self, id, file_name, size=None, content_type=None,
                 data=None, creation_time=None, last_change_time=None, **kw):

        if creation_time is not None:
            creation_time = parsetime(creation_time)
        if last_change_time is not None:
            last_change_time = parsetime(last_change_time)

        for k, v in kw.items():
            setattr(self, k, v)

        super().__init__(
            id=id, filename=file_name, size=size, mimetype=content_type,
            data=data, created=creation_time, modified=last_change_time)

    def __str__(self):
        if self.size is not None:
            if self.size < 1024*1024:
                size = '{}K'.format(round(self.size / 1024.0, 2))
            else:
                size = '{}M'.format(round(self.size / 1024*1024.0, 2))

            return 'Attachment: [{}] [{}] ({}, {})'.format(self.id, self.summary, size, self.mimetype)
        else:
            return 'Attachment: [{}] [{}]'.format(self.id, self.summary)

    @decompress
    def read(self):
        return base64.b64decode(self.data)
