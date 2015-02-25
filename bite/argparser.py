from argparse import ArgumentParser, SUPPRESS, Action, ArgumentError, ArgumentTypeError
import fileinput
import imp
import os
import shlex
import sys

import bite

def string_list(s):
    if sys.stdin.isatty() or s != '-':
        return [item for item in s.split(',') if item != ""]
    else:
        return s

def id_list(s):
    if sys.stdin.isatty() or s != '-':
        try:
            return [int(item) for item in s.split(',')]
        except:
            if item == '-':
                raise ArgumentTypeError("'-' is only valid when piping data in")
            else:
                raise ArgumentTypeError('invalid ID value: {}'.format(item))
    else:
        return s

def ids(s):
    if sys.stdin.isatty() or s != '-':
        try:
            return int(s)
        except:
            if s == '-':
                raise ArgumentTypeError("'-' is only valid when piping data in")
            else:
                raise ArgumentTypeError('invalid ID value: {}'.format(s))
    else:
        return s

def existing_file(s):
    if not os.path.exists(s):
        msg = '"{}" does not exist'.format(s)
        raise ArgumentTypeError(msg)
    return s

class parse_file(Action):
    def __call__(self, parser, namespace, values, option_string=None):
        lines = (shlex.split(line.strip()) for line in values)
        setattr(namespace, self.dest, lines)

class parse_stdin(Action):
    def __call__(self, parser, namespace, values, option_string=None):
        if values is not None and len(values) == 1 and values[0] == '-':
            if not sys.stdin.isatty():
                if option_string is None:
                    option = (self.dest, self.dest)
                else:
                    option = (self.dest, option_string)
                try:
                    stdin = getattr(namespace, 'stdin')
                    parser.error('argument {}: data from standard input '
                                 'already being used for argument {}'.format(option[1], stdin[1]))
                except AttributeError:
                    setattr(namespace, 'stdin', option)
                    # read args from standard input for specified option
                    values = [x.strip() for x in sys.stdin.readlines() if x.strip() != '']
                    sys.stdin = open('/dev/tty')
        setattr(namespace, self.dest, values)

def _import(filename):
    (path, name) = os.path.split(filename)
    (name, ext) = os.path.splitext(name)

    (file, filename, data) = imp.find_module(name, [path])
    return imp.load_module(name, file, filename, data)

class parse_filters(Action):
    def __call__(self, parser, namespace, values, option_string=None):
        filters = []

        for filter_name in values.split(','):
            module_name, _, fcn_name = filter_name.partition(':')
            if fcn_name == "":
                fcn_name = module_name
                module_name = namespace.connection

            try:
                module = _import(os.path.join(bite.CONFIG_DIR, 'python', module_name))
            except ImportError as e:
                parser.error(e)

            try:
                filters.append(getattr(module, fcn_name))
            except AttributeError as e:
                parser.error('No function "{}" in module "{}"'.format(fcn_name, module_name))

        setattr(namespace, self.dest, filters)

class parse_append(Action):
    def __call__(self, parser, namespace, values, option_string=None):
        if not isinstance(values, list):
            values = [values]
        current = getattr(namespace, self.dest)
        if current is None:
            setattr(namespace, self.dest, values)
        else:
            current.extend(values)

class ParseArgs(ArgumentParser):
    """
    ArgumentParser subclass that suppresses unicode string prefixes on the output message.
    """
    def _check_value(self, action, value):
        # converted value must be one of the choices (if specified)
        if action.choices is not None and value not in action.choices:
            tup = value, ', '.join(map(unicode, action.choices))
            msg = ('invalid choice: {} (choose from {})').format(*tup)
            raise ArgumentError(action, msg)

class ParseInitialArgs(ArgumentParser):
    """
    ArgumentParser subclass that modifies the argument parsing
    scheme to stop at the first positional argument.

    This is used to allow multiple shortcuts (like -c or -h) at both the
    general level and the method level. Otherwise, the argparse module
    wouldn't allow two of the same shortcuts to exist at the same time.
    """
    def _parse_known_args(self, arg_strings, namespace):
        # replace arg strings that are file references
        if self.fromfile_prefix_chars is not None:
            arg_strings = self._read_args_from_files(arg_strings)

        # map all mutually exclusive arguments to the other arguments
        # they can't occur with
        action_conflicts = {}
        for mutex_group in self._mutually_exclusive_groups:
            group_actions = mutex_group._group_actions
            for i, mutex_action in enumerate(mutex_group._group_actions):
                conflicts = action_conflicts.setdefault(mutex_action, [])
                conflicts.extend(group_actions[:i])
                conflicts.extend(group_actions[i + 1:])

        # find all option indices, and determine the arg_string_pattern
        # which has an 'O' if there is an option at an index,
        # an 'A' if there is an argument, or a '-' if there is a '--'
        option_string_indices = {}
        arg_string_pattern_parts = []
        arg_strings_iter = iter(arg_strings)
        for i, arg_string in enumerate(arg_strings_iter):

            # all args after -- are non-options
            if arg_string == '--':
                arg_string_pattern_parts.append('-')
                for arg_string in arg_strings_iter:
                    arg_string_pattern_parts.append('A')

            # otherwise, add the arg to the arg strings
            # and note the index if it was an option
            else:
                option_tuple = self._parse_optional(arg_string)
                if option_tuple is None:
                    pattern = 'A'
                else:
                    option_string_indices[i] = option_tuple
                    pattern = 'O'
                arg_string_pattern_parts.append(pattern)

        # join the pieces together to form the pattern
        arg_strings_pattern = ''.join(arg_string_pattern_parts)

        # converts arg strings to the appropriate and then takes the action
        seen_actions = set()
        seen_non_default_actions = set()

        def take_action(action, argument_strings, option_string=None):
            seen_actions.add(action)
            argument_values = self._get_values(action, argument_strings)

            # error if this argument is not allowed with other previously
            # seen arguments, assuming that actions that use the default
            # value don't really count as "present"
            if argument_values is not action.default:
                seen_non_default_actions.add(action)
                for conflict_action in action_conflicts.get(action, []):
                    if conflict_action in seen_non_default_actions:
                        msg = _('not allowed with argument {}')
                        action_name = _get_action_name(conflict_action)
                        raise ArgumentError(action, msg % action_name)

            # take the action if we didn't receive a SUPPRESS value
            # (e.g. from a default)
            if argument_values is not SUPPRESS:
                action(self, namespace, argument_values, option_string)

        # function to convert arg_strings into an optional action
        def consume_optional(start_index):

            # get the optional identified at this index
            option_tuple = option_string_indices[start_index]
            action, option_string, explicit_arg = option_tuple

            # identify additional optionals in the same arg string
            # (e.g. -xyz is the same as -x -y -z if no args are required)
            match_argument = self._match_argument
            action_tuples = []
            while True:

                # if we found no optional action, skip it
                if action is None:
                    extras.append(arg_strings[start_index])
                    return start_index + 1

                # if there is an explicit argument, try to match the
                # optional's string arguments to only this
                if explicit_arg is not None:
                    arg_count = match_argument(action, 'A')

                    # if the action is a single-dash option and takes no
                    # arguments, try to parse more single-dash options out
                    # of the tail of the option string
                    chars = self.prefix_chars
                    if arg_count == 0 and option_string[1] not in chars:
                        action_tuples.append((action, [], option_string))
                        char = option_string[0]
                        option_string = char + explicit_arg[0]
                        new_explicit_arg = explicit_arg[1:] or None
                        optionals_map = self._option_string_actions
                        if option_string in optionals_map:
                            action = optionals_map[option_string]
                            explicit_arg = new_explicit_arg
                        else:
                            msg = _('ignored explicit argument {}')
                            raise ArgumentError(action, msg % explicit_arg)

                    # if the action expect exactly one argument, we've
                    # successfully matched the option; exit the loop
                    elif arg_count == 1:
                        stop = start_index + 1
                        args = [explicit_arg]
                        action_tuples.append((action, args, option_string))
                        break

                    # error if a double-dash option did not use the
                    # explicit argument
                    else:
                        msg = _('ignored explicit argument {}')
                        raise ArgumentError(action, msg % explicit_arg)

                # if there is no explicit argument, try to match the
                # optional's string arguments with the following strings
                # if successful, exit the loop
                else:
                    start = start_index + 1
                    selected_patterns = arg_strings_pattern[start:]
                    arg_count = match_argument(action, selected_patterns)
                    stop = start + arg_count
                    args = arg_strings[start:stop]
                    action_tuples.append((action, args, option_string))
                    break

            # add the Optional to the list and return the index at which
            # the Optional's string args stopped
            assert action_tuples
            for action, args, option_string in action_tuples:
                take_action(action, args, option_string)
            return stop

        # the list of Positionals left to be parsed; this is modified
        # by consume_positionals()
        positionals = self._get_positional_actions()

        # function to convert arg_strings into positional actions
        def consume_positionals(start_index):
            # match as many Positionals as possible
            match_partial = self._match_arguments_partial
            selected_pattern = arg_strings_pattern[start_index:]
            arg_counts = match_partial(positionals, selected_pattern)

            # slice off the appropriate arg strings for each Positional
            # and add the Positional and its args to the list
            for action, arg_count in zip(positionals, arg_counts):
                args = arg_strings[start_index: start_index + arg_count]
                start_index += arg_count
                take_action(action, args)

            # slice off the Positionals that we just parsed and return the
            # index at which the Positionals' string args stopped
            positionals[:] = positionals[len(arg_counts):]
            return start_index

        # consume Positionals and Optionals alternately, until we have
        # passed the last option string
        extras = []
        start_index = 0
        if option_string_indices:
            max_option_string_index = max(option_string_indices)
        else:
            max_option_string_index = -1
        while start_index <= max_option_string_index:

            # consume any Positionals preceding the next option
            next_option_string_index = min([
                index
                for index in option_string_indices
                if index >= start_index])
            if start_index != next_option_string_index:
                positionals_end_index = consume_positionals(start_index)

                # only try to parse the next optional if we didn't consume
                # the option string during the positionals parsing
                if positionals_end_index >= start_index:
                    start_index = positionals_end_index
                    break
                else:
                    start_index = positionals_end_index

            # if we consumed all the positionals we could and we're not
            # at the index of an option string, there were extra arguments
            if start_index not in option_string_indices:
                strings = arg_strings[start_index:next_option_string_index]
                extras.extend(strings)
                start_index = next_option_string_index

            # consume the next optional and any arguments for it
            start_index = consume_optional(start_index)

        # consume any positionals following the last Optional
        stop_index = consume_positionals(start_index)

        # if we didn't consume all the argument strings, there were extras
        extras.extend(arg_strings[stop_index:])

        # if we didn't use all the Positional objects, there were too few
        # arg strings supplied.
        if positionals:
            self.error(_('too few arguments'))

        # make sure all required actions were present
        for action in self._actions:
            if action.required:
                if action not in seen_actions:
                    name = _get_action_name(action)
                    self.error(_('argument {} is required') % name)

        # make sure all required groups had one option present
        for group in self._mutually_exclusive_groups:
            if group.required:
                for action in group._group_actions:
                    if action in seen_non_default_actions:
                        break

                # if no actions were used, report the error
                else:
                    names = [_get_action_name(action)
                             for action in group._group_actions
                             if action.help is not SUPPRESS]
                    msg = _('one of the arguments {} is required')
                    self.error(msg % ' '.join(names))

        # return the updated namespace and the extra arguments
        return namespace, extras
