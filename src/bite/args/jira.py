from .. import args


class JiraOpts(args.ServiceOpts):
    """Jira options."""

    _service = 'jira'


@args.subcmd(JiraOpts)
class Search(args.PagedSearch):

    def add_args(self):
        super().add_args()

        self.person = self.parser.add_argument_group('Person related')
        self.person.add_argument(
            '-a', '--assigned-to', type='str_list', action='parse_stdin',
            help=f'user the {self.service.item.type} is assigned to')
        self.person.add_argument(
            '-r', '--creator', type='str_list', action='parse_stdin',
            help=f'user who created the {self.service.item.type}')
        self.person.add_argument(
            '--watchers', type='int range', metavar='LOWER[-UPPER]',
            help=f'{self.service.item.type} with a specified number of watchers')

        self.time = self.parser.add_argument_group('Time related')
        self.time.add_argument(
            '-c', '--created', type='time interval', metavar='TIME_INTERVAL',
            help=f'{self.service.item.type}s created within a specified time interval')
        self.time.add_argument(
            '-m', '--modified', type='time interval', metavar='TIME_INTERVAL',
            help=f'{self.service.item.type}s modified within a specified time interval')

        self.attr = self.parser.add_argument_group('Attribute related')
        self.attr.add_argument(
            '--votes', type='int range', metavar='LOWER[-UPPER]',
            help=f'{self.service.item.type}s with a specified number of votes')
