#!/usr/bin/env python

from __future__ import print_function

import argparse
import array
import atexit
import math
import os
import pathlib
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import threading
import traceback
from select import select
from queue import Queue
from subprocess import PIPE, STDOUT, Popen
from typing import BinaryIO, Optional, Tuple

if sys.version_info[0] == 2:
    IS_PY_3 = False
else:
    IS_PY_3 = True

test_aot = False
# Available targets:
#   "aarch64" "aarch64_vfp" "armv7" "armv7_vfp" "thumbv7" "thumbv7_vfp"
#   "riscv32" "riscv32_ilp32f" "riscv32_ilp32d" "riscv64" "riscv64_lp64f" "riscv64_lp64d"
test_target = "x86_64"

debug_file = None
log_file = None

# to save the register module with self-define name
temp_file_repo = []

# to save the mapping of module files in /tmp by name
temp_module_table = {}

# AOT compilation options mapping
aot_target_options_map = {
    "i386": ["--target=i386"],
    "x86_32": ["--target=i386"],
    # cf. https://github.com/bytecodealliance/wasm-micro-runtime/issues/3035
    "x86_64": ["--target=x86_64", "--cpu=skylake", "--size-level=0"],
    "aarch64": ["--target=aarch64", "--target-abi=eabi", "--cpu=cortex-a53"],
    "aarch64_vfp": ["--target=aarch64", "--target-abi=gnueabihf", "--cpu=cortex-a53"],
    "armv7": ["--target=armv7", "--target-abi=eabi", "--cpu=cortex-a9", "--cpu-features=-neon"],
    "armv7_vfp": ["--target=armv7", "--target-abi=gnueabihf", "--cpu=cortex-a9"],
    "thumbv7": ["--target=thumbv7", "--target-abi=eabi", "--cpu=cortex-a9", "--cpu-features=-neon,-vfpv3"],
    "thumbv7_vfp": ["--target=thumbv7", "--target-abi=gnueabihf", "--cpu=cortex-a9", "--cpu-features=-neon"],
    "riscv32": ["--target=riscv32", "--target-abi=ilp32", "--cpu=generic-rv32", "--cpu-features=+m,+a,+c"],
    "riscv32_ilp32f": ["--target=riscv32", "--target-abi=ilp32f", "--cpu=generic-rv32", "--cpu-features=+m,+a,+c,+f"],
    "riscv32_ilp32d": ["--target=riscv32", "--target-abi=ilp32d", "--cpu=generic-rv32", "--cpu-features=+m,+a,+c,+f,+d"],
    # RISCV64 requires -mcmodel=medany, which can be set by --size-level=1
    "riscv64": ["--target=riscv64", "--target-abi=lp64", "--cpu=generic-rv64", "--cpu-features=+m,+a,+c", "--size-level=1"],
    "riscv64_lp64f": ["--target=riscv64", "--target-abi=lp64f", "--cpu=generic-rv64", "--cpu-features=+m,+a,+c,+f", "--size-level=1"],
    "riscv64_lp64d": ["--target=riscv64", "--target-abi=lp64d", "--cpu=generic-rv64", "--cpu-features=+m,+a,+c,+f,+d", "--size-level=1"],
    "xtensa": ["--target=xtensa"],
}

# AOT compilation options mapping for XIP mode
aot_target_options_map_xip = {
    # avoid l32r relocations for xtensa
    "xtensa": ["--mllvm=-mtext-section-literals"],
    "riscv32_ilp32f": ["--enable-builtin-intrinsics=i64.common,f64.common,f32.const,f64.const,f64xi32,f64xi64,f64_promote_f32,f32_demote_f64"],
}

def debug(data):
    if debug_file:
        debug_file.write(data)
        debug_file.flush()

def log(data, end='\n'):
    if log_file:
        log_file.write(data + end)
        log_file.flush()
    print(data, end=end)
    sys.stdout.flush()

def create_tmp_file(prefix: str, suffix: str) -> str:
    return tempfile.NamedTemporaryFile(prefix=prefix, suffix=suffix, delete=False).name

# TODO: do we need to support '\n' too
import platform

if platform.system().find("CYGWIN_NT") >= 0:
    # TODO: this is weird, is this really right on Cygwin?
    sep = "\n\r\n"
else:
    sep = "\r\n"
rundir = None


class AsyncStreamReader:
    def __init__(self, stream: BinaryIO) -> None:
        self._queue = Queue()
        self._reader_thread = threading.Thread(
            daemon=True,
            target=AsyncStreamReader._stdout_reader,
            args=(self._queue, stream))
        self._reader_thread.start()

    def read(self) -> Optional[bytes]:
        return self._queue.get()

    def cleanup(self) -> None:
        self._reader_thread.join()

    @staticmethod
    def _stdout_reader(queue: Queue, stdout: BinaryIO) -> None:
        while True:
            try:
                queue.put(stdout.read(1))
            except ValueError as e:
                if stdout.closed:
                    queue.put(None)
                    break
                raise e


class Runner():
    def __init__(self, args, no_pty=False):
        self.no_pty = no_pty

        # Cleanup child process on exit
        atexit.register(self.cleanup)

        self.process = None
        env = os.environ
        env['TERM'] = 'dumb'
        env['INPUTRC'] = '/dev/null'
        env['PERL_RL'] = 'false'
        if no_pty:
            self.process = Popen(args, bufsize=0,
                           stdin=PIPE, stdout=PIPE, stderr=STDOUT,
                           env=env)
            self.stdin = self.process.stdin
            self.stdout = self.process.stdout
        else:
            import fcntl
            # Pseudo-TTY and terminal manipulation
            import pty
            import termios
            # Use tty to setup an interactive environment
            master, slave = pty.openpty()

            # Set terminal size large so that readline will not send
            # ANSI/VT escape codes when the lines are long.
            buf = array.array('h', [100, 200, 0, 0])
            fcntl.ioctl(master, termios.TIOCSWINSZ, buf, True)

            self.process = Popen(args, bufsize=0,
                           stdin=slave, stdout=slave, stderr=STDOUT,
                           preexec_fn=os.setsid,
                           env=env)
            # Now close slave so that we will get an exception from
            # read when the child exits early
            # http://stackoverflow.com/questions/11165521
            os.close(slave)
            self.stdin = os.fdopen(master, 'r+b', 0)
            self.stdout = self.stdin

        if platform.system().lower() == "windows":
            self._stream_reader = AsyncStreamReader(self.stdout)
        else:
            self._stream_reader = None

        self.buf = ""

    def _read_stdout_byte(self) -> Tuple[bool, Optional[bytes]]:
        if self._stream_reader:
            return True, self._stream_reader.read()
        else:
            # select doesn't work on file descriptors on Windows.
            # however, this method is much faster than using
            # queue, so we keep it for non-windows platforms.
            [outs,_,_] = select([self.stdout], [], [], 1)
            if self.stdout in outs:
                try:
                    stdout_byte = self.stdout.read(1)
                except ValueError:
                    return True, None
                except OSError:
                    return True, None
                except Exception as e:
                    print("Exception: ", e)
                    return False, None
                return True, stdout_byte
            else:
                return False, None

    def read_to_prompt(self, prompts, timeout):
        wait_until = time.time() + timeout
        while time.time() < wait_until:
            has_value, read_byte = self._read_stdout_byte()
            if not has_value:
                continue
            if not read_byte:
                # EOF on macOS ends up here.
                break
            read_byte = read_byte.decode('utf-8') if IS_PY_3 else read_byte

            debug(read_byte)
            if self.no_pty:
                self.buf += read_byte.replace('\n', '\r\n')
            else:
                self.buf += read_byte
            self.buf = self.buf.replace('\r\r', '\r')

            # filter the prompts
            for prompt in prompts:
                pattern = re.compile(prompt)
                match = pattern.search(self.buf)
                if match:
                    end = match.end()
                    buf = self.buf[0:end-len(prompt)]
                    self.buf = self.buf[end:]
                    return buf

        log("left read_to_prompt() because of timeout")
        return None

    def writeline(self, str):
        str_to_write = str + '\n'
        str_to_write = bytes(
            str_to_write, 'utf-8') if IS_PY_3 else str_to_write

        self.stdin.write(str_to_write)

    def cleanup(self):
        atexit.unregister(self.cleanup)

        if self.process:
            try:
                self.writeline("__exit__")
                time.sleep(.020)
                self.process.kill()
            except OSError:
                pass
            except IOError:
                pass
            self.process = None
            self.stdin.close()
            if self.stdin != self.stdout:
                self.stdout.close()
            self.stdin = None
            self.stdout = None
            if not IS_PY_3:
                sys.exc_clear()
            if self._stream_reader:
                self._stream_reader.cleanup()

def assert_prompt(runner, prompts, timeout, is_need_execute_result):
    # Wait for the initial prompt
    header = runner.read_to_prompt(prompts, timeout=timeout)
    if not header and is_need_execute_result:
        log(" ---------- will terminate cause the case needs result while there is none inside of buf. ----------")
        raise Exception("get nothing from Runner")
    if not header == None:
        if header:
            log("Started with:\n%s" % header)
    else:
        log("Did not one of following prompt(s): %s" % repr(prompts))
        log("    Got      : %s" % repr(r.buf))
        raise Exception("Did not one of following prompt(s)")


### WebAssembly specific

parser = argparse.ArgumentParser(
        description="Run a test file against a WebAssembly interpreter")
parser.add_argument('--wast2wasm', type=str,
        default=os.environ.get("WAST2WASM", "wast2wasm"),
        help="Path to wast2wasm program")
parser.add_argument('--interpreter', type=str,
        default=os.environ.get("IWASM_CMD", "iwasm"),
        help="Path to WebAssembly interpreter")
parser.add_argument('--aot-compiler', type=str,
        default=os.environ.get("WAMRC_CMD", "wamrc"),
        help="Path to WebAssembly AoT compiler")

parser.add_argument('--no_cleanup', action='store_true',
        help="Keep temporary *.wasm files")

parser.add_argument('--rundir',
        help="change to the directory before running tests")
parser.add_argument('--start-timeout', default=30, type=int,
        help="default timeout for initial prompt")
parser.add_argument('--start-fail-timeout', default=2, type=int,
        help="default timeout for initial prompt (when expected to fail)")
parser.add_argument('--test-timeout', default=20, type=int,
        help="default timeout for each individual test action")
parser.add_argument('--no-pty', action='store_true',
        help="Use direct pipes instead of pseudo-tty")
parser.add_argument('--log-file', type=str,
        help="Write messages to the named file in addition the screen")
parser.add_argument('--log-dir', type=str,
        help="The log directory to save the case file if test failed")
parser.add_argument('--debug-file', type=str,
        help="Write all test interaction the named file")

parser.add_argument('test_file', type=argparse.FileType('r'),
        help="a WebAssembly *.wast test file")

parser.add_argument('--aot', action='store_true',
        help="Test with AOT")

parser.add_argument('--target', type=str,
        default="x86_64",
        help="Set running target")

parser.add_argument('--sgx', action='store_true',
        help="Test SGX")

parser.add_argument('--simd', default=False, action='store_true',
        help="Enable SIMD")

parser.add_argument('--xip', default=False, action='store_true',
        help="Enable XIP")

parser.add_argument('--eh', default=False, action='store_true',
        help="Enable Exception Handling")

parser.add_argument('--multi-module', default=False, action='store_true',
        help="Enable Multi-thread")

parser.add_argument('--multi-thread', default=False, action='store_true',
        help="Enable Multi-thread")

parser.add_argument('--gc', default=False, action='store_true',
        help='Test with GC')

parser.add_argument('--extended-const', action='store_true',
                       help='Enable extended const expression feature')

parser.add_argument('--memory64', default=False, action='store_true',
        help='Test with Memory64')

parser.add_argument('--multi-memory', default=False, action='store_true',
        help='Test with multi-memory(with multi-module auto enabled)')

parser.add_argument('--qemu', default=False, action='store_true',
        help="Enable QEMU")

parser.add_argument('--qemu-firmware', default='', help="Firmware required by qemu")

parser.add_argument('--verbose', default=False, action='store_true',
        help='show more logs')

# regex patterns of tests to skip
C_SKIP_TESTS = ()
PY_SKIP_TESTS = (
        # names.wast
        'invoke \"~!',
        # conversions.wast
        '18446742974197923840.0',
        '18446744073709549568.0',
        '9223372036854775808',
        'reinterpret_f.*nan',
        # endianness
        '.const 0x1.fff' )

def read_forms(string):
    forms = []
    form = ""
    depth = 0
    line = 0
    pos = 0
    while pos < len(string):
        # Keep track of line number
        if string[pos] == '\n': line += 1

        # Handle top-level elements
        if depth == 0:
            # Add top-level comments
            if string[pos:pos+2] == ";;":
                end = string.find("\n", pos)
                if end == -1: end == len(string)
                forms.append(string[pos:end])
                pos = end
                continue

            # TODO: handle nested multi-line comments
            if string[pos:pos+2] == "(;":
                # Skip multi-line comment
                end = string.find(";)", pos)
                if end == -1:
                    raise Exception("mismatch multiline comment on line %d: '%s'" % (
                        line, string[pos:pos+80]))
                pos = end+2
                continue

            # Ignore whitespace between top-level forms
            if string[pos] in (' ', '\n', '\t'):
                pos += 1
                continue

        # Read a top-level form
        if string[pos] == '(': depth += 1
        if string[pos] == ')': depth -= 1
        if depth == 0 and not form:
            raise Exception("garbage on line %d: '%s'" % (
                line, string[pos:pos+80]))
        form += string[pos]
        if depth == 0 and form:
            forms.append(form)
            form = ""
        pos += 1
    return forms

def get_module_exp_from_assert(string):
    depth = 0
    pos = 0
    module = ""
    exception = ""
    start_record = False
    result = []
    while pos < len(string):
        # record from the " (module "
        if string[pos:pos+7] == "(module":
            start_record = True
        if start_record:
            if string[pos] == '(' : depth += 1
            if string[pos] == ')' : depth -= 1
            module += string[pos]
            # if we get all (module ) .
            if depth == 0 and module:
                result.append(module)
                start_record = False
        # get expected exception
        if string[pos] == '"':
            end = string.find("\"", pos+1)
            if end != -1:
                end_rel = string.find("\"",end+1)
                if end_rel == -1:
                    result.append(string[pos+1:end])
        pos += 1
    return result

def string_to_unsigned(number_in_string, lane_type):
    if not lane_type in ['i8x16', 'i16x8', 'i32x4', 'i64x2']:
        raise Exception("invalid value {} and type {} and lane_type {}".format(number_in_string, type, lane_type))

    number = int(number_in_string, 16) if '0x' in number_in_string else int(number_in_string)

    if "i8x16" == lane_type:
        if number < 0:
            packed = struct.pack('b', number)
            number = struct.unpack('B', packed)[0]
    elif "i16x8" == lane_type:
        if number < 0:
            packed = struct.pack('h', number)
            number = struct.unpack('H', packed)[0]
    elif "i32x4" == lane_type:
        if number < 0:
            packed = struct.pack('i', number)
            number = struct.unpack('I', packed)[0]
    else: # "i64x2" == lane_type:
        if number < 0:
            packed = struct.pack('q', number)
            number = struct.unpack('Q', packed)[0]

    return number

def cast_v128_to_i64x2(numbers, type, lane_type):
    numbers = [n.replace("_", "") for n in numbers]

    if "i8x16" == lane_type:
        assert(16 == len(numbers)), "{} should like {}".format(numbers, lane_type)
        # str -> int
        numbers = [string_to_unsigned(n, lane_type) for n in numbers]
        # i8 -> i64
        packed = struct.pack(16 * "B", *numbers)
    elif "i16x8" == lane_type:
        assert(8 == len(numbers)), "{} should like {}".format(numbers, lane_type)
        # str -> int
        numbers = [string_to_unsigned(n, lane_type) for n in numbers]
        # i16 -> i64
        packed = struct.pack(8 * "H", *numbers)
    elif "i32x4" == lane_type:
        assert(4 == len(numbers)), "{} should like {}".format(numbers, lane_type)
        # str -> int
        numbers = [string_to_unsigned(n, lane_type) for n in numbers]
        # i32 -> i64
        packed = struct.pack(4 * "I", *numbers)
    elif "i64x2" == lane_type:
        assert(2 == len(numbers)), "{} should like {}".format(numbers, lane_type)
        # str -> int
        numbers = [string_to_unsigned(n, lane_type) for n in numbers]
        # i64 -> i64
        packed = struct.pack(2 * "Q", *numbers)
    elif "f32x4" == lane_type:
        assert(4 == len(numbers)), "{} should like {}".format(numbers, lane_type)
        # str -> int
        numbers = [parse_simple_const_w_type(n, "f32")[0] for n in numbers]
        # f32 -> i64
        packed = struct.pack(4 * "f", *numbers)
    elif "f64x2" == lane_type:
        assert(2 == len(numbers)), "{} should like {}".format(numbers, lane_type)
        # str -> int
        numbers = [parse_simple_const_w_type(n, "f64")[0] for n in numbers]
        # f64 -> i64
        packed = struct.pack(2 * "d", *numbers)
    else:
        raise Exception("invalid value {} and type {} and lane_type {}".format(numbers, type, lane_type))

    assert(packed)
    unpacked = struct.unpack("Q Q", packed)
    return unpacked, f"[{unpacked[0]:#x} {unpacked[1]:#x}]:{lane_type}:v128"

def parse_simple_const_w_type(number, type):
    number = number.replace('_', '')
    number = re.sub(r"nan\((ind|snan)\)", "nan", number)
    if type in ["i32", "i64"]:
        number = int(number, 16) if '0x' in number else int(number)
        return number, "0x{:x}:{}".format(number, type) \
                   if number >= 0 \
                   else "-0x{:x}:{}".format(0 - number, type)
    elif type in ["f32", "f64"]:
        if "nan:" in number:
            return float('nan'), "nan:{}".format(type)
        else:
            number = float.fromhex(number) if '0x' in number else float(number)
            return number, "{:.7g}:{}".format(number, type)
    elif type == "ref.null":
        if number == "func":
            return "func", "func:ref.null"
        elif number == "extern":
            return "extern", "extern:ref.null"
        elif number == "any":
            return "any", "any:ref.null"
        else:
            raise Exception("invalid value {} and type {}".format(number, type))
    elif type == "ref.extern":
        number = int(number, 16) if '0x' in number else int(number)
        return number, "0x{:x}:ref.extern".format(number)
    elif type == "ref.host":
        number = int(number, 16) if '0x' in number else int(number)
        return number, "0x{:x}:ref.host".format(number)
    else:
        raise Exception("invalid value {} and type {}".format(number, type))

def parse_assertion_value(val):
    """
    Parse something like:
    "ref.null extern" in (assert_return (invoke "get-externref" (i32.const 0)) (ref.null extern))
    "ref.extern 1" in (assert_return (invoke "get-externref" (i32.const 1)) (ref.extern 1))
    "i32.const 0" in (assert_return (invoke "is_null-funcref" (i32.const 1)) (i32.const 0))

    in summary:
    type.const (sub-type) (val1 val2 val3 val4) ...
    type.const val
    ref.extern val
    ref.null ref_type
    ref.array
    ref.struct
    ref.func
    ref.i31
    """
    if not val:
        return None, ""

    splitted = re.split(r'\s+', val)
    splitted = [s for s in splitted if s]
    type = splitted[0].split(".")[0]
    lane_type = splitted[1] if len(splitted) > 2 else ""
    numbers = splitted[2:] if len(splitted) > 2 else splitted[1:]

    if type in ["i32", "i64", "f32", "f64"]:
        return parse_simple_const_w_type(numbers[0], type)
    elif type == "ref":
        if splitted[0] in ["ref.array", "ref.struct", "ref.func", "ref.i31"]:
            return splitted[0]
        # need to distinguish between "ref.null" and "ref.extern"
        return parse_simple_const_w_type(numbers[0], splitted[0])
    else:
        return cast_v128_to_i64x2(numbers, type, lane_type)

def int2uint32(i):
    return i & 0xffffffff

def int2int32(i):
    val = i & 0xffffffff
    if val & 0x80000000:
        return val - 0x100000000
    else:
        return val

def int2uint64(i):
    return i & 0xffffffffffffffff

def int2int64(i):
    val = i & 0xffffffffffffffff
    if val & 0x8000000000000000:
        return val - 0x10000000000000000
    else:
        return val


def num_repr(i):
    if isinstance(i, int) or isinstance(i, long):
        return re.sub("L$", "", hex(i))
    else:
        return "%.16g" % i

def hexpad16(i):
    return "0x%04x" % i

def hexpad24(i):
    return "0x%06x" % i

def hexpad32(i):
    return "0x%08x" % i

def hexpad64(i):
    return "0x%016x" % i

def invoke(r, args, cmd):
    r.writeline(cmd)

    return r.read_to_prompt(['\r\nwebassembly> ', '\nwebassembly> '],
                            timeout=args.test_timeout)

def vector_value_comparison(out, expected):
    """
    out likes "<number number>:v128"
    expected likes "[number number]:v128"
    """
    # print("vector value comparision {} vs {}".format(out, expected))

    out_val, out_type = out.split(':')
    # <number nubmer> => number number
    out_val = out_val[1:-1]

    expected_val, lane_type, expected_type = expected.split(':')
    # [number nubmer] => number number
    expected_val = expected_val[1:-1]

    assert("v128" == out_type), "out_type should be v128"
    assert("v128" == expected_type), "expected_type should be v128"

    if out_type != expected_type:
        return False

    out_val = out_val.split(" ")
    expected_val = expected_val.split(" ")

    # since i64x2
    out_packed = struct.pack("QQ", int(out_val[0], 16), int(out_val[1], 16))
    expected_packed = struct.pack("QQ",
        int(expected_val[0]) if not "0x" in expected_val[0] else int(expected_val[0], 16),
        int(expected_val[1]) if not "0x" in expected_val[1] else int(expected_val[1], 16))

    if lane_type in ["i8x16", "i16x8", "i32x4", "i64x2"]:
        return out_packed == expected_packed
    else:
        assert(lane_type in ["f32x4", "f64x2"]), "unexpected lane_type"

        if "f32x4" == lane_type:
            out_unpacked = struct.unpack("ffff", out_packed)
            expected_unpacked = struct.unpack("ffff", expected_packed)
        else:
            out_unpacked = struct.unpack("dd", out_packed)
            expected_unpacked = struct.unpack("dd", expected_packed)

        out_is_nan = [math.isnan(o) for o in out_unpacked]
        expected_is_nan = [math.isnan(e) for e in expected_unpacked]
        if any(out_is_nan):
            nan_comparision = [o == e for o, e in zip(out_is_nan, expected_is_nan)]
            if all(nan_comparision):
                print(f"Pass NaN comparision")
                return True

        # print(f"compare {out_unpacked} and {expected_unpacked}")
        result = [o == e for o, e in zip(out_unpacked, expected_unpacked)]
        if not all(result):
            result = [
                "{:.7g}".format(o) == "{:.7g}".format(e)
                for o, e in zip(out_unpacked, expected_packed)
            ]

        return all(result)


def simple_value_comparison(out, expected):
    """
    compare out of simple types which may like val:i32, val:f64 and so on
    """
    if expected == "2.360523e+13:f32" and out == "2.360522e+13:f32":
        # one case in float_literals.wast, due to float precision of python
        return True

    if expected == "1.797693e+308:f64" and out == "inf:f64":
        # one case in float_misc.wast:
        # (assert_return (invoke "f64.add" (f64.const 0x1.fffffffffffffp+1023)
        #                                  (f64.const 0x1.fffffffffffffp+969))
        #                                  (f64.const 0x1.fffffffffffffp+1023))
        # the add result in x86_32 is inf
        return True

    out_val, out_type = out.split(':')
    expected_val, expected_type = expected.split(':')

    if not out_type == expected_type:
        return False

    out_val, _ = parse_simple_const_w_type(out_val, out_type)
    expected_val, _ = parse_simple_const_w_type(expected_val, expected_type)

    if out_val == expected_val \
        or (math.isnan(out_val) and math.isnan(expected_val)):
        return True

    if "i32" == expected_type:
        out_val_binary = struct.pack('I', out_val) if out_val > 0 \
                            else struct.pack('i', out_val)
        expected_val_binary = struct.pack('I', expected_val) \
                                if expected_val > 0 \
                                    else struct.pack('i', expected_val)
    elif "i64" == expected_type:
        out_val_binary = struct.pack('Q', out_val) if out_val > 0 \
                            else struct.pack('q', out_val)
        expected_val_binary = struct.pack('Q', expected_val) \
                                if expected_val > 0 \
                                    else struct.pack('q', expected_val)
    elif "f32" == expected_type:
        out_val_binary = struct.pack('f', out_val)
        expected_val_binary = struct.pack('f', expected_val)
    elif "f64" == expected_type:
        out_val_binary = struct.pack('d', out_val)
        expected_val_binary = struct.pack('d', expected_val)
    elif "ref.extern" == expected_type:
        out_val_binary = out_val
        expected_val_binary = expected_val
    elif "ref.host" == expected_type:
        out_val_binary = out_val
        expected_val_binary = expected_val
    else:
        assert(0), "unknown 'expected_type' {}".format(expected_type)

    if out_val_binary == expected_val_binary:
        return True

    if expected_type in ["f32", "f64"]:
        # compare with a lower precision
        out_str = "{:.7g}".format(out_val)
        expected_str = "{:.7g}".format(expected_val)
        if out_str == expected_str:
            return True

    return False

def value_comparison(out, expected):
    if out == expected:
        return True

    if not expected:
        return False

    if not out in ["ref.array", "ref.struct", "ref.func", "ref.any", "ref.i31"]:
        assert(':' in out), "out should be in a form likes numbers:type, but {}".format(out)
    if not expected in ["ref.array", "ref.struct", "ref.func", "ref.any", "ref.i31"]:
        assert(':' in expected), "expected should be in a form likes numbers:type, but {}".format(expected)

    if 'v128' in out:
        return vector_value_comparison(out, expected)
    else:
        return simple_value_comparison(out, expected)

def is_result_match_expected(out, expected):
    # compare value instead of comparing strings of values
    return value_comparison(out, expected)

def test_assert(r, opts, mode, cmd, expected):
    log("Testing(%s) %s = %s" % (mode, cmd, expected))
    out = invoke(r, opts, cmd)
    if '\n' in out or ' ' in out:
        outs = [''] + out.split('\n')[1:]
        out = outs[-1]

    if mode=='trap':
        o = re.sub('^Exception: ', '', out)
        e = re.sub('^Exception: ', '', expected)
        if o.find(e) >= 0 or e.find(o) >= 0:
            return True

    if mode=='exhaustion':
        o = re.sub('^Exception: ', '', out)
        expected = 'Exception: stack overflow'
        e = re.sub('^Exception: ', '', expected)
        if o.find(e) >= 0 or e.find(o) >= 0:
            return True

    # wasm-exception thrown out of function call, not a trap
    if mode=='wasmexception':
        o = re.sub('^Exception: ', '', out)
        e = re.sub('^Exception: ', '', expected)
        if o.find(e) >= 0 or e.find(o) >= 0:
            return True

    ## 0x9:i32,-0x1:i32 -> ['0x9:i32', '-0x1:i32']
    expected_list = re.split(r',', expected)
    out_list = re.split(r',', out)
    if len(expected_list) != len(out_list):
        raise Exception("Failed:\n Results count incorrect:\n expected: '%s'\n  got: '%s'" % (expected, out))
    for i in range(len(expected_list)):
        if not is_result_match_expected(out_list[i], expected_list[i]):
            raise Exception("Failed:\n Result %d incorrect:\n expected: '%s'\n  got: '%s'" % (i, expected_list[i], out_list[i]))

    return True

def test_assert_return(r, opts, form):
    """
    m. to search a pattern like (assert_return (invoke function_name ... ) ...)
    n. to search a pattern like (assert_return (invoke $module_name function_name ... ) ...)
    """
    # params, return
    m = re.search(r'^\(assert_return\s+\(invoke\s+"((?:[^"]|\\\")*)"\s+(\(.*\))\s*\)\s*(\(.*\))\s*\)\s*$', form, re.S)
    # judge if assert_return cmd includes the module name
    n = re.search(r'^\(assert_return\s+\(invoke\s+\$((?:[^\s])*)\s+"((?:[^"]|\\\")*)"\s+(\(.*\))\s*\)\s*(\(.*\))\s*\)\s*$', form, re.S)

    # print("assert_return with {}".format(form))

    if not m:
        # no params, return
        m = re.search(r'^\(assert_return\s+\(invoke\s+"((?:[^"]|\\\")*)"\s*\)\s+()(\(.*\))\s*\)\s*$', form, re.S)
    if not m:
        # params, no return
        m = re.search(r'^\(assert_return\s+\(invoke\s+"([^"]*)"\s+(\(.*\))()\s*\)\s*\)\s*$', form, re.S)
    if not m:
        # no params, no return
        m = re.search(r'^\(assert_return\s+\(invoke\s+"([^"]*)"\s*()()\)\s*\)\s*$', form, re.S)
    if not m:
        # params, return
        if not n:
            # no params, return
            n = re.search(r'^\(assert_return\s+\(invoke\s+\$((?:[^\s])*)\s+"((?:[^"]|\\\")*)"\s*\)\s+()(\(.*\))\s*\)\s*$', form, re.S)
        if not n:
            # params, no return
            n = re.search(r'^\(assert_return\s+\(invoke\s+\$((?:[^\s])*)\s+"([^"]*)"\s+(\(.*\))()\s*\)\s*\)\s*$', form, re.S)
        if not n:
            # no params, no return
            n = re.search(r'^\(assert_return\s+\(invoke\s+\$((?:[^\s])*)\s+"([^"]*)"*()()\)\s*\)\s*$', form, re.S)
    if not m and not n:
        if re.search(r'^\(assert_return\s+\(get.*\).*\)$', form, re.S):
            log("ignoring assert_return get")
            return
        else:
            raise Exception("unparsed assert_return: '%s'" % form)
    if m and not n:
        func = m.group(1)
        if ' ' in func:
            func = func.replace(' ', '\\')

        # Note: 'as-memory.grow-first' doesn't actually grow memory.
        # (thus not in this list)
        if opts.qemu and opts.target == 'xtensa' and func in {'as-memory.grow-value', 'as-memory.grow-size', 'as-memory.grow-last', 'as-memory.grow-everywhere'}:
            log("ignoring memory.grow test")
            return

        if m.group(2) == '':
            args = []
        else:
            #args = [re.split(r' +', v)[1].replace('_', "") for v in re.split(r"\)\s*\(", m.group(2)[1:-1])]
            # split arguments with ')spaces(', remove leading and tailing ) and (
            args_type_and_value = re.split(r'\)\s+\(', m.group(2)[1:-1])
            args_type_and_value = [s.replace('_', '') for s in args_type_and_value]
            # args are in two forms:
            # f32.const -0x1.000001fffffffffffp-50
            # v128.const i32x4 0 0 0 0
            args = []
            for arg in args_type_and_value:
                # remove leading and tailing spaces, it might confuse following assertions
                arg = arg.strip()
                splitted = re.split(r'\s+', arg)
                splitted = [s for s in splitted if s]

                if splitted[0] in ["i32.const", "i64.const"]:
                    assert(2 == len(splitted)), "{} should have two parts".format(splitted)
                    # in wast 01234 means 1234
                    # in c 0123 means 83 in oct
                    number, _ = parse_simple_const_w_type(splitted[1], splitted[0][:3])
                    args.append(str(number))
                elif splitted[0] in ["f32.const", "f64.const"]:
                    # let strtof or strtod handle original arguments
                    assert(2 == len(splitted)), "{} should have two parts".format(splitted)
                    args.append(splitted[1])
                elif "v128.const" == splitted[0]:
                    assert(len(splitted) > 2), "{} should have more than two parts".format(splitted)
                    numbers, _ = cast_v128_to_i64x2(splitted[2:], 'v128', splitted[1])

                    assert(len(numbers) == 2), "has to reform arguments into i64x2"
                    args.append(f"{numbers[0]:#x}\\{numbers[1]:#x}")
                elif "ref.null" == splitted[0]:
                    args.append("null")
                elif "ref.extern" == splitted[0]:
                    number, _ = parse_simple_const_w_type(splitted[1], splitted[0])
                    args.append(str(number))
                elif "ref.host" == splitted[0]:
                    number, _ = parse_simple_const_w_type(splitted[1], splitted[0])
                    args.append(str(number))
                else:
                    assert(0), "an unkonwn parameter type"

        if m.group(3) == '':
            returns= []
        else:
            returns = re.split(r"\)\s*\(", m.group(3)[1:-1])
        # processed numbers in strings
        if len(returns) == 1 and returns[0] in ["ref.array", "ref.struct", "ref.i31",
                                                "ref.eq", "ref.any", "ref.extern",
                                                "ref.func", "ref.null"]:
            expected = [returns[0]]
        elif len(returns) == 1 and returns[0] in ["func:ref.null", "any:ref.null",
                                                  "extern:ref.null"]:
            expected = [returns[0]]
        else:
            expected = [parse_assertion_value(v)[1] for v in returns]
        expected = ",".join(expected)

        test_assert(r, opts, "return", "%s %s" % (func, " ".join(args)), expected)
    elif not m and n:
        module = temp_module_table[n.group(1)].split(".wasm")[0]
        # assume the cmd is (assert_return(invoke $ABC "func")).
        # run the ABC.wasm firstly
        if test_aot:
            r = compile_wasm_to_aot(module+".wasm", module+".aot", True, opts, r)
            try:
                assert_prompt(r, ['Compile success'], opts.start_timeout, False)
            except:
                _, exc, _ = sys.exc_info()
                log("Run wamrc failed:\n  got: '%s'" % r.buf)
                raise Exception("Run wamrc failed 1")
        r = run_wasm_with_repl(module+".wasm", module+".aot" if test_aot else module, opts, r)
        # Wait for the initial prompt
        try:
            assert_prompt(r, ['webassembly> '], opts.start_timeout, False)
        except:
            _, exc, _ = sys.exc_info()
            raise Exception("Failed:\n  expected: '%s'\n  got: '%s'" % \
                            (repr(exc), r.buf))
        func = n.group(2)
        if ' ' in func:
            func = func.replace(' ', '\\')

        if n.group(3) == '':
            args=[]
        else:
            # convert (ref.null extern/func) into (ref.null null)
            n1 = n.group(3).replace("(ref.null extern)", "(ref.null null)")
            n1 = n1.replace("ref.null func)", "(ref.null null)")
            args = [re.split(r' +', v)[1] for v in re.split(r"\)\s*\(", n1[1:-1])]

        _, expected = parse_assertion_value(n.group(4)[1:-1])
        test_assert(r, opts, "return", "%s %s" % (func, " ".join(args)), expected)

def test_assert_trap(r, opts, form):
    # params
    m = re.search(r'^\(assert_trap\s+\(invoke\s+"([^"]*)"\s+(\(.*\))\s*\)\s*"([^"]+)"\s*\)\s*$', form)
    # judge if assert_return cmd includes the module name
    n = re.search(r'^\(assert_trap\s+\(invoke\s+\$((?:[^\s])*)\s+"([^"]*)"\s+(\(.*\))\s*\)\s*"([^"]+)"\s*\)\s*$', form, re.S)
    if not m:
        # no params
        m = re.search(r'^\(assert_trap\s+\(invoke\s+"([^"]*)"\s*()\)\s*"([^"]+)"\s*\)\s*$', form)
    if not m:
        if not n:
            # no params
            n = re.search(r'^\(assert_trap\s+\(invoke\s+\$((?:[^\s])*)\s+"([^"]*)"\s*()\)\s*"([^"]+)"\s*\)\s*$', form, re.S)
    if not m and not n:
        raise Exception("unparsed assert_trap: '%s'" % form)

    if m and not n:
        func = m.group(1)
        if m.group(2) == '':
            args = []
        else:
            # convert (ref.null extern/func) into (ref.null null)
            m1 = m.group(2).replace("(ref.null extern)", "(ref.null null)")
            m1 = m1.replace("ref.null func)", "(ref.null null)")
            args = [re.split(r' +', v)[1] for v in re.split(r"\)\s*\(", m1[1:-1])]

        expected = "Exception: %s" % m.group(3)
        test_assert(r, opts, "trap", "%s %s" % (func, " ".join(args)), expected)

    elif not m and n:
        module = n.group(1)
        module = tempfile.gettempdir() + "/" + module

        # will trigger the module named in assert_return(invoke $ABC).
        # run the ABC.wasm firstly
        if test_aot:
            r = compile_wasm_to_aot(module+".wasm", module+".aot", True, opts, r)
            try:
                assert_prompt(r, ['Compile success'], opts.start_timeout, False)
            except:
                _, exc, _ = sys.exc_info()
                log("Run wamrc failed:\n  got: '%s'" % r.buf)
                raise Exception("Run wamrc failed 2")
        r = run_wasm_with_repl(module+".wasm", module+".aot" if test_aot else module, opts, r)
        # Wait for the initial prompt
        try:
            assert_prompt(r, ['webassembly> '], opts.start_timeout, False)
        except:
            _, exc, _ = sys.exc_info()
            raise Exception("Failed:\n  expected: '%s'\n  got: '%s'" % \
                            (repr(exc), r.buf))

        func = n.group(2)
        if n.group(3) == '':
            args = []
        else:
            args = [re.split(r' +', v)[1] for v in re.split(r"\)\s*\(", n.group(3)[1:-1])]
        expected = "Exception: %s" % n.group(4)
        test_assert(r, opts, "trap", "%s %s" % (func, " ".join(args)), expected)

def test_assert_exhaustion(r,opts,form):
    # params
    m = re.search(r'^\(assert_exhaustion\s+\(invoke\s+"([^"]*)"\s+(\(.*\))\s*\)\s*"([^"]+)"\s*\)\s*$', form)
    if not m:
        # no params
        m = re.search(r'^\(assert_exhaustion\s+\(invoke\s+"([^"]*)"\s*()\)\s*"([^"]+)"\s*\)\s*$', form)
    if not m:
        raise Exception("unparsed assert_exhaustion: '%s'" % form)
    func = m.group(1)
    if m.group(2) == '':
        args = []
    else:
        args = [re.split(r' +', v)[1] for v in re.split(r"\)\s*\(", m.group(2)[1:-1])]
    expected = "Exception: %s\n" % m.group(3)
    test_assert(r, opts, "exhaustion", "%s %s" % (func, " ".join(args)), expected)


# added to support WASM_ENABLE_EXCE_HANDLING
def test_assert_wasmexception(r,opts,form):
    # params

    # ^
    #     \(assert_exception\s+
    #         \(invoke\s+"([^"]+)"\s+
    #            (\(.*\))\s*
    #            ()
    #         \)\s*
    #     \)\s*
    # $
    m = re.search(r'^\(assert_exception\s+\(invoke\s+"([^"]+)"\s+(\(.*\))\s*\)\s*\)\s*$', form)
    if not m:
        # no params

        # ^
        #       \(assert_exception\s+
        #           \(invoke\s+"([^"]+)"\s*
        #               ()
        #           \)\s*
        #       \)\s*
        # $
        m = re.search(r'^\(assert_exception\s+\(invoke\s+"([^"]+)"\s*()\)\s*\)\s*$', form)
    if not m:
        raise Exception("unparsed assert_exception: '%s'" % form)
    func = m.group(1) # function name
    if m.group(2) == '': # arguments
        args = []
    else:
        args = [re.split(r' +', v)[1] for v in re.split(r"\)\s*\(", m.group(2)[1:-1])]

    expected = "Exception: uncaught wasm exception\n"
    test_assert(r, opts, "wasmexception", "%s %s" % (func, " ".join(args)), expected)

def do_invoke(r, opts, form):
    # params
    m = re.search(r'^\(invoke\s+"([^"]+)"\s+(\(.*\))\s*\)\s*$', form)
    if not m:
        # no params
        m = re.search(r'^\(invoke\s+"([^"]+)"\s*()\)\s*$', form)
    if not m:
        raise Exception("unparsed invoke: '%s'" % form)
    func = m.group(1)

    if ' ' in func:
        func = func.replace(' ', '\\')

    if m.group(2) == '':
        args = []
    else:
        args = [re.split(r' +', v)[1] for v in re.split(r"\)\s*\(", m.group(2)[1:-1])]

    log("Invoking %s(%s)" % (
        func, ", ".join([str(a) for a in args])))

    invoke(r, opts, "%s %s" % (func, " ".join(args)))

def skip_test(form, skip_list):
    for s in skip_list:
        if re.search(s, form):
            return True
    return False

def compile_wast_to_wasm(form, wast_tempfile, wasm_tempfile, opts):
    log("Writing WAST module to '%s'" % wast_tempfile)
    with open(wast_tempfile, 'w') as file:
        file.write(form)
    log("Compiling WASM to '%s'" % wasm_tempfile)

    # default arguments
    if opts.gc:
        cmd = [opts.wast2wasm, "-u", "-d", wast_tempfile, "-o", wasm_tempfile]
    elif opts.eh:
        cmd = [opts.wast2wasm, "--enable-threads", "--no-check", "--enable-exceptions", "--enable-tail-call", wast_tempfile, "-o", wasm_tempfile ]
    elif opts.memory64:
        cmd = [opts.wast2wasm, "--enable-memory64", "--no-check", wast_tempfile, "-o", wasm_tempfile ]
    elif opts.multi_memory:
        cmd = [opts.wast2wasm, "--enable-multi-memory", "--no-check", wast_tempfile, "-o", wasm_tempfile ]
    elif opts.extended_const:
        cmd = [opts.wast2wasm, "--enable-extended-const", "--no-check", wast_tempfile, "-o", wasm_tempfile ]
    else:
        # `--enable-multi-memory` for a case in memory.wast but doesn't require runtime support
        cmd = [opts.wast2wasm, "--enable-multi-memory", "--enable-threads", "--no-check",
               wast_tempfile, "-o", wasm_tempfile ]

    # remove reference-type and bulk-memory enabling options since a WABT
    # commit 30c1e983d30b33a8004b39fd60cbd64477a7956c
    # Enable reference types by default (#1729)

    log("Running: %s" % " ".join(cmd))
    try:
        subprocess.check_call(cmd)
    except Exception as e:
        print(e)
        return False

    return True

def compile_wasm_to_aot(wasm_tempfile, aot_tempfile, runner, opts, r, output = 'default'):
    log("Compiling '%s' to '%s'" % (wasm_tempfile, aot_tempfile))
    cmd = [opts.aot_compiler]

    if test_target in aot_target_options_map:
        cmd += aot_target_options_map[test_target]

    if opts.sgx:
        cmd.append("-sgx")

    if not opts.simd:
        cmd.append("--disable-simd")

    if opts.xip:
        cmd.append("--xip")
        if test_target in aot_target_options_map_xip:
            cmd += aot_target_options_map_xip[test_target]

    if opts.multi_thread:
        cmd.append("--enable-multi-thread")

    if opts.gc:
        cmd.append("--enable-gc")
        cmd.append("--enable-tail-call")

    if opts.extended_const:
        cmd.append("--enable-extended-const")

    if output == 'object':
        cmd.append("--format=object")
    elif output == 'ir':
        cmd.append("--format=llvmir-opt")

    # disable llvm link time optimization as it might convert
    # code of tail call into code of dead loop, and stack overflow
    # exception isn't thrown in several cases
    cmd.append("--disable-llvm-lto")

    # Bounds checks is disabled by default for 64-bit targets, to
    # use the hardware based bounds checks. But it is not supported
    # in QEMU with NuttX and in memory64 mode.
    # Enable bounds checks explicitly for all targets if running in QEMU or all targets
    # running in memory64 mode.
    if opts.qemu or opts.memory64:
        cmd.append("--bounds-checks=1")

    cmd += ["-o", aot_tempfile, wasm_tempfile]

    log("Running: %s" % " ".join(cmd))
    if not runner:
        subprocess.check_call(cmd)
    else:
        if (r != None):
            r.cleanup()
        r = Runner(cmd, no_pty=opts.no_pty)
        return r

def run_wasm_with_repl(wasm_tempfile, aot_tempfile, opts, r):
    tmpfile = aot_tempfile if test_aot else wasm_tempfile
    log("Starting interpreter for module '%s'" % tmpfile)

    if opts.qemu:
        tmpfile = f"/tmp/{os.path.basename(tmpfile)}"

    cmd_iwasm = [opts.interpreter, "--heap-size=0", "--repl"]
    if opts.multi_module:
        cmd_iwasm.append("--module-path=" + (tempfile.gettempdir() if not opts.qemu else "/tmp" ))
    if opts.gc:
        # our tail-call implementation is known broken.
        # work it around by using a huge stack.
        # cf. https://github.com/bytecodealliance/wasm-micro-runtime/issues/2231
        cmd_iwasm.append("--stack-size=10485760")  # 10MB (!)
    else:
        if opts.aot:
            # Note: aot w/o gc doesn't require the interpreter stack at all.
            # Note: 1 is the minimum value we can specify because 0 means
            # the default.
            cmd_iwasm.append("--stack-size=1")
        else:
            cmd_iwasm.append("--stack-size=131072")  # 128KB
    if opts.verbose:
        cmd_iwasm.append("-v=5")
    cmd_iwasm.append(tmpfile)

    if opts.qemu:
        if opts.qemu_firmware == '':
            raise Exception("QEMU firmware missing")

        if opts.target.startswith("aarch64"):
            cmd = "qemu-system-aarch64 -cpu cortex-a53 -nographic -machine virt,virtualization=on,gic-version=3 -net none -chardev stdio,id=con,mux=on -serial chardev:con -mon chardev=con,mode=readline -kernel".split()
            cmd.append(opts.qemu_firmware)
        elif opts.target.startswith("thumbv7"):
            cmd = "qemu-system-arm -semihosting -M sabrelite -m 1024 -smp 1 -nographic -kernel".split()
            cmd.append(opts.qemu_firmware)
        elif opts.target.startswith("riscv32"):
            cmd = "qemu-system-riscv32 -semihosting -M virt,aclint=on -cpu rv32 -smp 1 -nographic -bios none -kernel".split()
            cmd.append(opts.qemu_firmware)
        elif opts.target.startswith("riscv64"):
            cmd = "qemu-system-riscv64 -semihosting -M virt,aclint=on -cpu rv64 -smp 1 -nographic -bios none -kernel".split()
            cmd.append(opts.qemu_firmware)
        elif opts.target.startswith("xtensa"):
            cmd = f"qemu-system-xtensa -semihosting -nographic -serial mon:stdio -machine esp32s3 -drive file={opts.qemu_firmware},if=mtd,format=raw".split()
        else:
            raise Exception("Unknwon target for QEMU: %s" % opts.target)

    else:
        cmd = cmd_iwasm

    log("Running: %s" % " ".join(cmd))
    if (r != None):
        r.cleanup()
    r = Runner(cmd, no_pty=opts.no_pty)

    if opts.qemu:
        r.read_to_prompt(['nsh> '], 10)
        r.writeline("mount -t hostfs -o fs={} /tmp".format(tempfile.gettempdir()))
        r.read_to_prompt(['nsh> '], 10)
        r.writeline(" ".join(cmd_iwasm))

    return r

def create_tmpfiles(file_name, test_aot, temp_file_repo):
    tempfiles = []

    tempfiles.append(create_tmp_file(file_name, ".wast"))
    tempfiles.append(create_tmp_file(file_name, ".wasm"))
    if test_aot:
        tempfiles.append(create_tmp_file(file_name, ".aot"))
    else:
        tempfiles.append(None)
    
    assert len(tempfiles) == 3, "tempfiles should have 3 elements"

    # add these temp file to temporal repo, will be deleted when finishing the test
    temp_file_repo.extend(tempfiles)
    return tempfiles

def test_assert_with_exception(form, wast_tempfile, wasm_tempfile, aot_tempfile, opts, r, loadable = True):
    details_inside_ast = get_module_exp_from_assert(form)
    log("module is ....'%s'"%details_inside_ast[0])
    log("exception is ....'%s'"%details_inside_ast[1])
    # parse the module
    module = details_inside_ast[0]
    expected = details_inside_ast[1]

    if not compile_wast_to_wasm(module, wast_tempfile, wasm_tempfile, opts):
        raise Exception("compile wast to wasm failed")

    if test_aot:
        r = compile_wasm_to_aot(wasm_tempfile, aot_tempfile, True, opts, r)
        if not loadable:
            return

        try:
            assert_prompt(r, ['Compile success'], opts.start_fail_timeout, True)
        except:
            _, exc, _ = sys.exc_info()
            if (r.buf.find(expected) >= 0):
                log("Out exception includes expected one, pass:")
                log("  Expected: %s" % expected)
                log("  Got: %s" % r.buf)
                return
            else:
                log("Run wamrc failed:\n  expected: '%s'\n  got: '%s'" % \
                    (expected, r.buf))
                raise Exception("Run wamrc failed 3")

    r = run_wasm_with_repl(wasm_tempfile, aot_tempfile if test_aot else None, opts, r)

    # Some module couldn't load so will raise an error directly, so shell prompt won't show here

    if loadable:
        # Wait for the initial prompt
        try:
            assert_prompt(r, ['webassembly> '], opts.start_fail_timeout, True)
        except:
            _, exc, _ = sys.exc_info()
            if (r.buf.find(expected) >= 0):
                log("Out exception includes expected one, pass:")
                log("  Expected: %s" %expected)
                log("  Got: %s" % r.buf)
            else:
                raise Exception("Failed:\n  expected: '%s'\n  got: '%s'" % \
                                (expected, r.buf))

def recently_added_wasm(temp_file_repo):
    for f in reversed(temp_file_repo):
        if not f:
            continue

        assert os.path.exists(f), f"temp file {f} should exist"
        
        if os.path.getsize(f) == 0:
            continue

        if f.endswith(".wasm"):
            return f


if __name__ == "__main__":
    opts = parser.parse_args(sys.argv[1:])
    # print('Input param :',opts)

    if opts.aot: test_aot = True
    # default x86_64
    test_target = opts.target

    if opts.rundir: os.chdir(opts.rundir)

    if opts.log_file:   log_file   = open(opts.log_file, "a")
    if opts.debug_file: debug_file = open(opts.debug_file, "a")

    if opts.interpreter.endswith(".py"):
        SKIP_TESTS = PY_SKIP_TESTS
    else:
        SKIP_TESTS = C_SKIP_TESTS

    case_file = pathlib.Path(opts.test_file.name)
    assert(case_file.exists()), f"Test file {case_file} doesn't exist"

    tmpfile_stem = case_file.stem + "_"

    ret_code = 0
    try:
        log("\n################################################")
        log("### Testing %s" % opts.test_file.name)
        log("################################################")
        forms = read_forms(opts.test_file.read())
        r = None

        for form in forms:
            # log("\n### Current Case is " + form + "\n")

            wast_tempfile, wasm_tempfile, aot_tempfile = create_tmpfiles(
                tmpfile_stem, test_aot, temp_file_repo)

            if ";;" == form[0:2]:
                log(form)
            elif skip_test(form, SKIP_TESTS):
                log("Skipping test: %s" % form[0:60])
            elif re.match(r"^\(assert_trap\s+\(module", form):
                test_assert_with_exception(form, wast_tempfile, wasm_tempfile, aot_tempfile if test_aot else None, opts, r)
            elif re.match(r"^\(assert_exhaustion\b.*", form):
                test_assert_exhaustion(r, opts, form)
            elif re.match(r"^\(assert_exception\b.*", form):
                test_assert_wasmexception(r, opts, form)
            elif re.match(r"^\(assert_unlinkable\b.*", form):
                test_assert_with_exception(form, wast_tempfile, wasm_tempfile, aot_tempfile if test_aot else None, opts, r, False)
            elif re.match(r"^\(assert_malformed\b.*", form):
                # remove comments in wast
                form,n = re.subn(";;.*\n", "", form)
                m = re.match(r"^\(assert_malformed\s*\(module binary\s*(\".*\").*\)\s*\"(.*)\"\s*\)$", form, re.DOTALL)

                if m:
                    # workaround: spec test changes error message to "malformed" while iwasm still use "invalid"
                    error_msg = m.group(2).replace("malformed", "invalid")
                    log("Testing(malformed)")
                    with open(wasm_tempfile, 'wb') as f:
                        s = m.group(1)
                        while s:
                            res = re.match(r"[^\"]*\"([^\"]*)\"(.*)", s, re.DOTALL)
                            if IS_PY_3:
                                context = res.group(1).replace("\\", "\\x").encode("latin1").decode("unicode-escape").encode("latin1")
                                f.write(context)
                            else:
                                f.write(res.group(1).replace("\\", "\\x").decode("string-escape"))
                            s = res.group(2)

                    # compile wasm to aot
                    if test_aot:
                        r = compile_wasm_to_aot(wasm_tempfile, aot_tempfile, True, opts, r)
                        try:
                            assert_prompt(r, ['Compile success'], opts.start_fail_timeout, True)
                        except:
                            _, exc, _ = sys.exc_info()
                            if (r.buf.find(error_msg) >= 0):
                                log("Out exception includes expected one, pass:")
                                log("  Expected: %s" % error_msg)
                                log("  Got: %s" % r.buf)
                                continue
                            # one case in binary.wast
                            elif (error_msg == "unexpected end of section or function"
                                  and r.buf.find("unexpected end")):
                                continue
                            # one case in binary.wast
                            elif (error_msg == "invalid value type"
                                  and r.buf.find("unexpected end")):
                                continue
                            # one case in binary.wast
                            elif (error_msg == "integer too large"
                                  and r.buf.find("tables cannot be shared")):
                                continue
                            # one case in binary.wast
                            elif (error_msg == "zero byte expected"
                                  and r.buf.find("unknown table")):
                                continue
                            # one case in binary.wast
                            elif (error_msg == "invalid section id"
                                  and r.buf.find("unexpected end of section or function")):
                                continue
                            # one case in binary.wast
                            elif (error_msg == "illegal opcode"
                                  and r.buf.find("unexpected end of section or function")):
                                continue
                            # one case in custom.wast
                            elif (error_msg == "length out of bounds"
                                  and r.buf.find("unexpected end")):
                                continue
                            # several cases in binary-leb128.wast
                            elif (error_msg == "integer representation too long"
                                  and r.buf.find("invalid section id")):
                                continue
                            else:
                                log("Run wamrc failed:\n  expected: '%s'\n  got: '%s'" % \
                                    (error_msg, r.buf))
                                raise Exception("Run wamrc failed 4")

                    r = run_wasm_with_repl(wasm_tempfile, aot_tempfile if test_aot else None, opts, r)

                elif re.match(r"^\(assert_malformed\s*\(module quote", form):
                    log("ignoring assert_malformed module quote")
                else:
                    log("unrecognized assert_malformed")
            elif re.match(r"^\(assert_return[_a-z]*_nan\b.*", form):
                log("ignoring assert_return_.*_nan")
                pass
            elif re.match(r".*\(invoke\s+\$\b.*", form):
                # invoke a particular named module's function
                if form.startswith("(assert_return"):
                    test_assert_return(r,opts,form)
                elif form.startswith("(assert_trap"):
                    test_assert_trap(r,opts,form)
            elif re.match(r"^\(module\b.*", form):
                # if the module includes the particular name startswith $
                m = re.search(r"^\(module\s+\$.\S+", form)
                if m:
                    # get module name
                    module_name = re.split(r'\$', m.group(0).strip())[1]
                    if module_name:
                        # create temporal files
                        temp_files = create_tmpfiles(module_name, test_aot, temp_file_repo)
                        if not compile_wast_to_wasm(form, temp_files[0], temp_files[1], opts):
                            raise Exception("compile wast to wasm failed")

                        if test_aot:
                            r = compile_wasm_to_aot(temp_files[1], temp_files[2], True, opts, r)
                            try:
                                assert_prompt(r, ['Compile success'], opts.start_timeout, False)
                            except:
                                _, exc, _ = sys.exc_info()
                                log("Run wamrc failed:\n  got: '%s'" % r.buf)
                                raise Exception("Run wamrc failed 5")
                        temp_module_table[module_name] = temp_files[1]
                        r = run_wasm_with_repl(temp_files[1], temp_files[2] if test_aot else None, opts, r)
                else:
                    if not compile_wast_to_wasm(form, wast_tempfile, wasm_tempfile, opts):
                        raise Exception("compile wast to wasm failed")

                    if test_aot:
                        r = compile_wasm_to_aot(wasm_tempfile, aot_tempfile, True, opts, r)
                        try:
                            assert_prompt(r, ['Compile success'], opts.start_timeout, False)
                        except:
                            _, exc, _ = sys.exc_info()
                            log("Run wamrc failed:\n  got: '%s'" % r.buf)
                            raise Exception("Run wamrc failed 6")

                    r = run_wasm_with_repl(wasm_tempfile, aot_tempfile if test_aot else None, opts, r)

                # Wait for the initial prompt
                try:
                    assert_prompt(r, ['webassembly> '], opts.start_timeout, False)
                except:
                    _, exc, _ = sys.exc_info()
                    raise Exception("Failed:\n  expected: '%s'\n  got: '%s'" % \
                                    (repr(exc), r.buf))

            elif re.match(r"^\(assert_return\b.*", form):
                assert(r), "iwasm repl runtime should be not null"
                test_assert_return(r, opts, form)
            elif re.match(r"^\(assert_trap\b.*", form):
                test_assert_trap(r, opts, form)
            elif re.match(r"^\(invoke\b.*", form):
                assert(r), "iwasm repl runtime should be not null"
                do_invoke(r, opts, form)
            elif re.match(r"^\(assert_invalid\b.*", form):
                # loading invalid module will raise an error directly, so shell prompt won't show here
                test_assert_with_exception(form, wast_tempfile, wasm_tempfile, aot_tempfile if test_aot else None, opts, r, False)
            elif re.match(r"^\(register\b.*", form):
                # get module's new name from the register cmd
                name_new =re.split(r'\"',re.search(r'\".*\"',form).group(0))[1]
                if not name_new:
                    # there is no name defined in register cmd
                    raise Exception(f"Not following register cmd pattern {form}")

                # assumption
                # - There exists a module in the form of (module $name).
                # - The nearest module in the form of (module), without $name, is the candidate for registration.
                recently_wasm = recently_added_wasm(temp_file_repo)
                if not name_new in temp_module_table:
                    print(temp_file_repo)
                    print(f"Module {name_new} is not found in temp_module_table. use the nearest module {recently_wasm}")

                for_registration = temp_module_table.get(name_new, recently_wasm)
                assert os.path.exists(for_registration), f"module {for_registration} is not found"

                new_module = os.path.join(tempfile.gettempdir(), name_new + ".wasm")
                # for_registration(tmpfile) --copy-> name_new.wasm
                shutil.copyfile(for_registration, new_module)

                # add new_module copied from the old into temp_file_repo[]
                temp_file_repo.append(new_module)

                if test_aot:
                    new_module_aot = os.path.join(tempfile.gettempdir(), name_new + ".aot")
                    try:
                        compile_wasm_to_aot(new_module, new_module_aot, None, opts, r)
                    except Exception as e:
                        raise Exception(f"compile wasm to aot failed. {e}")
                    # add aot module into temp_file_repo[]
                    temp_file_repo.append(new_module_aot)
            else:
                raise Exception("unrecognized form '%s...'" % form[0:40])
    except Exception as e:
        traceback.print_exc()
        print("THE FINAL EXCEPTION IS {}".format(e))
        ret_code = 101

        try:
            shutil.copyfile(wasm_tempfile, os.path.join(opts.log_dir, os.path.basename(wasm_tempfile)))

            if opts.aot or opts.xip:
                shutil.copyfile(aot_tempfile, os.path.join(opts.log_dir,os.path.basename(aot_tempfile)))
                if "indirect-mode" in str(e):
                    compile_wasm_to_aot(wasm_tempfile, aot_tempfile, None, opts, None, "object")
                    shutil.copyfile(aot_tempfile, os.path.join(opts.log_dir,os.path.basename(aot_tempfile)+'.o'))
                    subprocess.check_call(["llvm-objdump", "-r", aot_tempfile])
                compile_wasm_to_aot(wasm_tempfile, aot_tempfile, None, opts, None, "ir")
                shutil.copyfile(aot_tempfile, os.path.join(opts.log_dir,os.path.basename(aot_tempfile)+".ir"))
        except Exception as e:
            print("Failed to copy files to log directory: %s" % e)
            ret_code = 102
    else:
        ret_code = 0
    finally:
        try:
            if not opts.no_cleanup:
                # remove the files under /tempfiles/ and copy of .wasm files
                log(f"Removing tmp*")
                # log(f"Removing {temp_file_repo}")

                for t in temp_file_repo:
                    # None and empty
                    if not t:
                        continue

                    if os.path.exists(t):
                        os.remove(t)
            else:
                log(f"Leaving tmp*")
                # log(f"Leaving {temp_file_repo}")
            
        except Exception as e:
            print("Failed to remove tempfiles: %s" % e)
            # ignore the exception
            ret_code = 0

        log(f"### End testing {opts.test_file.name} with {ret_code}")
        sys.exit(ret_code)
