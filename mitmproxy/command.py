"""
    This module manages and invokes typed commands.
"""
import functools
import inspect
import sys
import textwrap
import types
import typing

import pyparsing

import mitmproxy.types
from mitmproxy import exceptions


def verify_arg_signature(f: typing.Callable, args: typing.Iterable[typing.Any], kwargs: dict) -> None:
    sig = inspect.signature(f)
    try:
        sig.bind(*args, **kwargs)
    except TypeError as v:
        raise exceptions.CommandError("command argument mismatch: %s" % v.args[0])


def typename(t: type) -> str:
    """
        Translates a type to an explanatory string.
    """
    if t == inspect._empty:  # type: ignore
        raise exceptions.CommandError("missing type annotation")
    to = mitmproxy.types.CommandTypes.get(t, None)
    if not to:
        raise exceptions.CommandError("unsupported type: %s" % getattr(t, "__name__", t))
    return to.display


def _empty_as_none(x: typing.Any) -> typing.Any:
    if x == inspect.Signature.empty:
        return None
    return x


class CommandParameter(typing.NamedTuple):
    display_name: str
    type: typing.Type


class Command:
    name: str
    manager: "CommandManager"
    signature: inspect.Signature
    help: typing.Optional[str]

    def __init__(self, manager: "CommandManager", name: str, func: typing.Callable) -> None:
        self.name = name
        self.manager = manager
        self.func = func
        self.signature = inspect.signature(self.func)

        if func.__doc__:
            txt = func.__doc__.strip()
            self.help = "\n".join(textwrap.wrap(txt))
        else:
            self.help = None

        # This fails with a CommandException if types are invalid
        for name, parameter in self.signature.parameters.items():
            t = parameter.annotation
            if not mitmproxy.types.CommandTypes.get(parameter.annotation, None):
                raise exceptions.CommandError(f"Argument {name} has an unknown type ({_empty_as_none(t)}) in {func}.")
        if self.return_type and not mitmproxy.types.CommandTypes.get(self.return_type, None):
            raise exceptions.CommandError(f"Return type has an unknown type ({self.return_type}) in {func}.")

    @property
    def return_type(self) -> typing.Optional[typing.Type]:
        return _empty_as_none(self.signature.return_annotation)

    @property
    def parameters(self) -> typing.List[CommandParameter]:
        """Returns a list of (display name, type) tuples."""
        ret = []
        for name, param in self.signature.parameters.items():
            if param.kind is param.VAR_POSITIONAL:
                name = f"*{name}"
            ret.append(CommandParameter(name, param.annotation))
        return ret

    def signature_help(self) -> str:
        params = " ".join(name for name, t in self.parameters)
        if self.return_type:
            ret = f" -> {typename(self.return_type)}"
        else:
            ret = ""
        return f"{self.name} {params}{ret}"

    def prepare_args(self, args: typing.Sequence[str]) -> inspect.BoundArguments:
        try:
            bound_arguments = self.signature.bind(*args)
        except TypeError as v:
            raise exceptions.CommandError(f"Command argument mismatch: {v.args[0]}")

        for name, value in bound_arguments.arguments.items():
            convert_to = self.signature.parameters[name].annotation
            bound_arguments.arguments[name] = parsearg(self.manager, value, convert_to)

        bound_arguments.apply_defaults()

        return bound_arguments

    def call(self, args: typing.Sequence[str]) -> typing.Any:
        """
        Call the command with a list of arguments. At this point, all
        arguments are strings.
        """
        bound_args = self.prepare_args(args)
        ret = self.func(*bound_args.args, **bound_args.kwargs)
        if ret is None and self.return_type is None:
            return
        typ = mitmproxy.types.CommandTypes.get(self.return_type)
        assert typ
        if not typ.is_valid(self.manager, typ, ret):
            raise exceptions.CommandError(
                f"{self.name} returned unexpected data - expected {typ.display}"
            )
        return ret


class ParseResult(typing.NamedTuple):
    value: str
    type: typing.Type
    valid: bool


class CommandManager:
    commands: typing.Dict[str, Command]

    def __init__(self, master):
        self.master = master
        self.commands = {}

        self.expr_parser = pyparsing.ZeroOrMore(
            pyparsing.QuotedString('"', escChar='\\', unquoteResults=False)
            | pyparsing.QuotedString("'", escChar='\\', unquoteResults=False)
            | pyparsing.Combine(pyparsing.Literal('"')
                                + pyparsing.Word(pyparsing.printables + " ")
                                + pyparsing.StringEnd())
            | pyparsing.Word(pyparsing.printables)
            | pyparsing.Word(" \r\n\t")
        ).leaveWhitespace()

    def collect_commands(self, addon):
        for i in dir(addon):
            if not i.startswith("__"):
                o = getattr(addon, i)
                try:
                    is_command = hasattr(o, "command_name")
                except Exception:
                    pass  # hasattr may raise if o implements __getattr__.
                else:
                    if is_command:
                        try:
                            self.add(o.command_name, o)
                        except exceptions.CommandError as e:
                            self.master.log.warn(
                                "Could not load command %s: %s" % (o.command_name, e)
                            )

    def add(self, path: str, func: typing.Callable):
        self.commands[path] = Command(self, path, func)

    @functools.lru_cache(maxsize=128)
    def parse_partial(
            self,
            cmdstr: str
    ) -> typing.Tuple[typing.Sequence[ParseResult], typing.Sequence[CommandParameter]]:
        """
        Parse a possibly partial command. Return a sequence of ParseResults and a sequence of remainder type help items.
        """

        parts: typing.List[str] = self.expr_parser.parseString(cmdstr)

        parsed: typing.List[ParseResult] = []
        next_params: typing.List[CommandParameter] = [
            CommandParameter("", mitmproxy.types.Cmd),
            CommandParameter("", mitmproxy.types.CmdArgs),
        ]
        for part in parts:
            if part.isspace():
                parsed.append(
                    ParseResult(
                        value=part,
                        type=mitmproxy.types.Space,
                        valid=True,
                    )
                )
                continue

            if next_params:
                expected_type: typing.Type = next_params.pop(0).type
            else:
                expected_type = mitmproxy.types.Unknown

            arg_is_known_command = (
                    expected_type == mitmproxy.types.Cmd and part in self.commands
            )
            arg_is_unknown_command = (
                    expected_type == mitmproxy.types.Cmd and part not in self.commands
            )
            command_args_following = (
                    next_params and next_params[0].type == mitmproxy.types.CmdArgs
            )
            if arg_is_known_command and command_args_following:
                next_params = self.commands[part].parameters + next_params[1:]
            if arg_is_unknown_command and command_args_following:
                next_params.pop(0)

            to = mitmproxy.types.CommandTypes.get(expected_type, None)
            valid = False
            if to:
                try:
                    to.parse(self, expected_type, part)
                except exceptions.TypeError:
                    valid = False
                else:
                    valid = True

            parsed.append(
                ParseResult(
                    value=part,
                    type=expected_type,
                    valid=valid,
                )
            )

        return parsed, next_params

    def call(self, command_name: str, *args: typing.Sequence[typing.Any]) -> typing.Any:
        """
        Call a command with native arguments. May raise CommandError.
        """
        if command_name not in self.commands:
            raise exceptions.CommandError("Unknown command: %s" % command_name)
        return self.commands[command_name].func(*args)

    def _call_strings(self, command_name: str, args: typing.Sequence[str]) -> typing.Any:
        """
        Call a command using a list of string arguments. May raise CommandError.
        """
        if command_name not in self.commands:
            raise exceptions.CommandError("Unknown command: %s" % command_name)

        return self.commands[command_name].call(args)

    def execute(self, cmdstr: str) -> typing.Any:
        """
        Execute a command string. May raise CommandError.
        """
        parts, _ = self.parse_partial(cmdstr)
        if not parts:
            raise exceptions.CommandError(f"Invalid command: {cmdstr!r}")
        command_name, *args = [
            unquote(part.value)
            for part in parts
            if part.type != mitmproxy.types.Space
        ]
        return self._call_strings(command_name, args)

    def dump(self, out=sys.stdout) -> None:
        cmds = list(self.commands.values())
        cmds.sort(key=lambda x: x.signature_help())
        for c in cmds:
            for hl in (c.help or "").splitlines():
                print("# " + hl, file=out)
            print(c.signature_help(), file=out)
            print(file=out)


def unquote(x: str) -> str:
    if x.startswith("'") and x.endswith("'"):
        return x[1:-1]
    if x.startswith('"') and x.endswith('"'):
        return x[1:-1]
    return x


def parsearg(manager: CommandManager, spec: str, argtype: type) -> typing.Any:
    """
        Convert a string to a argument to the appropriate type.
    """
    t = mitmproxy.types.CommandTypes.get(argtype, None)
    if not t:
        raise exceptions.CommandError(f"Unsupported argument type: {argtype}")
    try:
        return t.parse(manager, argtype, spec)
    except exceptions.TypeError as e:
        raise exceptions.CommandError from e


def command(name: typing.Optional[str]):
    def decorator(function):
        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            verify_arg_signature(function, args, kwargs)
            return function(*args, **kwargs)

        wrapper.__dict__["command_name"] = name or function.__name__
        return wrapper

    return decorator


def argument(name, type):
    """
        Set the type of a command argument at runtime. This is useful for more
        specific types such as mitmproxy.types.Choice, which we cannot annotate
        directly as mypy does not like that.
    """

    def decorator(f: types.FunctionType) -> types.FunctionType:
        assert name in f.__annotations__
        f.__annotations__[name] = type
        return f

    return decorator
