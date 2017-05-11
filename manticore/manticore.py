import os
import sys
import time
import types
import logging
import binascii
import tempfile
import functools

from multiprocessing import Manager, Pool
from multiprocessing import Process

from threading import Timer

from elftools.elf.elffile import ELFFile
from elftools.elf.sections import SymbolTableSection

from .core.executor import Executor
from .core.state import State, TerminateState
from .core.parser import parse
from .core.smtlib import solver, Expression, Operators, SolverException, Array, ConstraintSet
from core.smtlib import BitVec, Bool
from .platforms import linux, decree, windows
from .utils.helpers import issymbolic
logger = logging.getLogger('MANTICORE')



class ProfilingResults(object):
    def __init__(self, raw_stats, instructions_executed):
        self.raw_stats = raw_stats
        self.instructions_executed = instructions_executed

        self.time_elapsed = raw_stats.total_tt

        self.loading_time = 0
        self.saving_time = 0
        self.solver_time = 0
        for (func_file, _, func_name), (_, _, _, func_time, _) in raw_stats.stats.iteritems():
            if func_name == 'restore':
                self.loading_time += func_time
            elif func_name == 'backup':
                self.saving_time += func_time
            elif func_file.endswith('solver.py') and 'setstate' not in func_name and 'getstate' not in func_name and 'ckl' not in func_name:
                self.solver_time += func_time



def makeDecree(args):
    constraints = ConstraintSet()
    model = decree.SDecree(constraints, ','.join(args.programs))
    initial_state = State(constraints, model)
    logger.info('Loading program %s', args.programs)

    #if args.data != '':
    #    logger.info('Starting with concrete input: {}'.format(args.data))
    model.input.transmit(args.data)
    model.input.transmit(initial_state.symbolicate_buffer('+'*14, label='RECEIVE'))
    return initial_state

def makeLinux(program, argv, env, concrete_start = ''):
    logger.info('Loading program %s', program)

    constraints = ConstraintSet()
    model = linux.SLinux(constraints, program, argv=argv, envp=env,
            symbolic_files=('symbolic.txt'))
    initial_state = State(constraints, model)

    if concrete_start != '':
        logger.info('Starting with concrete input: {}'.format(concrete_start))

    for i, arg in enumerate(argv):
        argv[i] = initial_state.symbolicate_buffer(arg, label='ARGV%d' % (i+1),
                string=True)

    for i, evar in enumerate(env):
        env[i] = initial_state.symbolicate_buffer(evar, label='ENV%d' % (i+1),
                string=True)

    # If any of the arguments or environment refer to symbolic values, re-
    # initialize the stack
    if any(issymbolic(x) for val in argv + env for x in val):
        model.setup_stack([program] + argv, env)

    model.input.transmit(concrete_start)

    #set stdin input...
    model.input.transmit(initial_state.symbolicate_buffer('+'*256, label='STDIN'))

    return initial_state 


def makeWindows(args):
    assert args.size is not None, "Need to specify buffer size"
    assert args.buffer is not None, "Need to specify buffer base address"
    logger.debug('Loading program %s', args.programs)
    additional_context = None
    if args.context:
        with open(args.context, "r") as addl_context_file:
            additional_context = cPickle.loads(addl_context_file.read())
            logger.debug('Additional context loaded with contents {}'.format(additional_context)) #DEBUG

    constraints = ConstraintSet()
    model = windows.SWindows(constraints, args.programs[0], additional_context, snapshot_folder=args.workspace)

    #This will interpret the buffer specification written in INTEL ASM. (It may dereference pointers)
    data_size = parse(args.size, model.current.read_bytes, model.current.read_register)
    data_ptr  = parse(args.buffer, model.current.read_bytes, model.current.read_register)

    logger.debug('Buffer at %x size %d bytes)', data_ptr, data_size)
    buf_str = "".join(model.current.read_bytes(data_ptr, data_size))
    logger.debug('Original buffer: %s', buf_str.encode('hex'))

    offset = args.offset 
    concrete_data = args.data.decode('hex')
    assert data_size >= offset + len(concrete_data)
    size = min(args.maxsymb, data_size - offset - len(concrete_data))
    symb = constraints.new_array(name='RAWMSG', index_max=size)

    model.current.write_bytes(data_ptr + offset, concrete_data)
    model.current.write_bytes(data_ptr + offset + len(concrete_data), [symb[i] for i in xrange(size)] )

    logger.debug('First %d bytes are left concrete', offset)
    logger.debug('followed by %d bytes of concrete start', len(concrete_data))
    hex_head = "".join(model.current.read_bytes(data_ptr, offset+len(concrete_data)))
    logger.debug('Hexdump head: %s', hex_head.encode('hex'))
    logger.debug('Total symbolic characters inserted: %d', size)
    logger.debug('followed by %d bytes of unmodified concrete bytes at end.', (data_size-offset-len(concrete_data))-size )
    hex_tail = "".join(map(chr, model.current.read_bytes(data_ptr+offset+len(concrete_data)+size, data_size-(offset+len(concrete_data)+size))))
    logger.debug('Hexdump tail: %s', hex_tail.encode('hex'))
    logger.info("Starting PC is: {:08x}".format(model.current.PC))

    return State(constraints, model)

def binary_type(path):
    '''
    Given a path to a binary, return a string representation of its type.
      i.e. ELF, PE, DECREE, QNX
    '''
    magic = None
    with open(path) as f:
        magic = f.read(4)

    if magic == '\x7fELF':
        return 'ELF'
    elif magic == 'MDMP':
        return 'PE'
    elif magic == '\x7fCGC':
        return 'DECREE'
    else:
        raise NotImplementedError("Binary {} not supported. Magic bytes: 0x{}".format(path, binascii.hexlify(magic)))

class Manticore(object):
    '''
    The central analysis object.

    :param str binary_path: Path to binary to analyze
    :param args: Arguments to provide to binary
    :type args: list[str]
    '''


    def __init__(self, binary_path, args=None):
        assert os.path.isfile(binary_path)

        args = [] if args is None else args

        self._binary = binary_path
        self._binary_type = binary_type(binary_path)
        self._argv = args # args.programs[1:]
        self._env = {}
        # Will be set to a temporary directory if not set before running start()
        self._workspace_path = None
        self._policy = 'random'
        self._coverage_file = None
        self._memory_errors = None
        self._should_profile = False
        self._workers = []
        # XXX(yan) '_args' will be removed soon; exists currently to ease porting
        self._args = args
        self._time_started = 0
        self._num_processes = 1
        self._begun_trace = False
        self._assertions = {}
        self._model_hooks = {}
        self._hooks = {}
        self._running = False
        self._arch = None
        self._concrete_data = ''
        self._dumpafter = 0
        self._maxstates = 0
        self._maxstorage = 0
        self._verbosity = 0

        manager = Manager()

        self.context = manager.dict()

        # XXX(yan) This is a bit obtuse; once PE support is updated this should
        # be refactored out
        if self._binary_type == 'ELF':
            self._binary_obj = ELFFile(file(self._binary))

        self._init_logging()

    def _init_logging(self): 

        def loggerSetState(logger, stateid):
            logger.filters[0].stateid = stateid

        class ContextFilter(logging.Filter):
            '''
            This is a filter which injects contextual information into the log.
            '''
            def filter(self, record):
                if hasattr(self, 'stateid') and isinstance(self.stateid, int):
                    record.stateid = '[%d]' % self.stateid
                else:
                    record.stateid = ''
                return True

        ctxfilter = ContextFilter()

        logging.basicConfig(format='%(asctime)s: [%(process)d]%(stateid)s %(name)s:%(levelname)s: %(message)s', stream=sys.stdout)

        for loggername in ['VISITOR', 'EXECUTOR', 'CPU', 'REGISTERS', 'SMT', 'MEMORY', 'MAIN', 'MODEL']:
            logging.getLogger(loggername).addFilter(ctxfilter)
            logging.getLogger(loggername).setState = types.MethodType(loggerSetState, logging.getLogger(loggername))
        
        logging.getLogger('SMT').setLevel(logging.INFO)
        logging.getLogger('MEMORY').setLevel(logging.INFO)
        logging.getLogger('LIBC').setLevel(logging.INFO)

    # XXX(yan): args is a temporary hack to include while we continue moving
    # non-Linux platforms to new-style arg handling.
    @property
    def args(self):
        return self._args

    @args.setter
    def args(self, args):
        self._args = args

    @property
    def should_profile(self):
        return self._should_profile

    @should_profile.setter
    def should_profile(self, enable_profiling):
        self._should_profile = enable_profiling

    @property
    def concrete_data(self):
        return self._concrete_data

    @concrete_data.setter
    def concrete_data(self, data):
        self._concrete_data = data

    @property
    def maxstates(self):
        return self._maxstates

    @maxstates.setter
    def maxstates(self, max_states):
        self._maxstates = max_states

    @property
    def dumpafter(self):
        return self._dumpafter

    @dumpafter.setter
    def dumpafter(self, dump_after):
        self._dumpafter = dump_after

    @property
    def maxstorage(self):
        return self._maxstorage

    @maxstorage.setter
    def maxstorage(self, max_storage):
        self._maxstorage = max_storage

    @property
    def verbosity(self):
        '''
        Convenience interface for setting logging verbosity to one of several predefined
        logging presets. Valid values: 0-5
        '''
        return self._verbosity

    @verbosity.setter
    def verbosity(self, setting):
        levels = [[],
                  [('MAIN', logging.INFO), ('EXECUTOR', logging.INFO)],
                  [('MAIN', logging.INFO), ('EXECUTOR', logging.DEBUG), ('MODEL', logging.DEBUG)],
                  [('MAIN', logging.INFO), ('EXECUTOR', logging.DEBUG), ('MODEL', logging.DEBUG), ('MEMORY', logging.DEBUG), ('CPU', logging.DEBUG)],
                  [('MAIN', logging.INFO), ('EXECUTOR', logging.DEBUG), ('MODEL', logging.DEBUG), ('MEMORY', logging.DEBUG), ('CPU', logging.DEBUG), ('REGISTERS', logging.DEBUG)],
                  [('MAIN', logging.INFO), ('EXECUTOR', logging.DEBUG), ('MODEL', logging.DEBUG), ('MEMORY', logging.DEBUG), ('CPU', logging.DEBUG), ('REGISTERS', logging.DEBUG), ('SMT', logging.DEBUG)]]
        # Takes a value and ensures it's in a certain range
        def clamp(val, minimum, maximum):
            return sorted((minimum, val, maximum))[1]

        clamped = clamp(setting, 0, len(levels) - 1)
        if clamped != setting:
            logger.debug("%s not between 0 and %d, forcing to %d", setting, len(levels) - 1, clamped)
        for log_type, level in levels[clamped]:
            logging.getLogger(log_type).setLevel(level)
            self._verbosity = setting

    def hook(self, pc):
        '''
        A decorator used to register a hook function for a given instruction address.
        Equivalent to calling :func:`~add_hook`.

        :param pc: Address of instruction to hook
        :type pc: int or None
        '''
        def decorator(f):
            self.add_hook(pc, f)
            return f
        return decorator

    def add_hook(self, pc, callback):
        '''
        Add a callback to be invoked on executing a program counter. Pass `None`
        for pc to invoke callback on every instruction. `callback` should be a callable
        that takes one :class:`~manticore.core.executor.State` argument.

        :param pc: Address of instruction to hook
        :type pc: int or None
        :param callable callback: Hook function
        '''
        if not (isinstance(pc, (int, long)) or pc is None):
            raise TypeError("pc must be either an int or None, not {}".format(pc.__class__.__name__))
        else:
            self._hooks.setdefault(pc, set()).add(callback)

    def _get_symbol_address(self, symbol):
        '''
        Return the address of |symbol| within the binary
        '''
        if self._binary_obj is None:
            return NotImplementedError("Symbols aren't supported")

        for section in self._binary_obj.iter_sections():
            if not isinstance(section, SymbolTableSection):
                continue

            symbols = section.get_symbol_by_name(symbol)
            if len(symbols) == 0:
                continue

            return symbols[0].entry['st_value']

    def _make_state(self, path):
        if self._binary_type == 'ELF':
            # Linux
            env = ['%s=%s'%(k,v) for k,v in self._env.items()]
            state = makeLinux(self._binary, self._argv, env, self._concrete_data)
        elif self._binary_type == 'PE':
            # Windows
            state = makeWindows(self._args)
        elif self._binary_type == 'DECREE':
            # Decree
            state = makeDecree(self._args)
        else:
            raise NotImplementedError("Binary {} not supported.".format(path))

        return state
        
    @property
    def workspace(self):
        if self._workspace_path is None:
            self._workspace_path = self._make_workspace()

        return self._workspace_path

    @workspace.setter
    def workspace(self, path):
        assert not self._running, "Can't set workspace if Manticore is running."

        if os.path.exists(path):
            assert os.path.isdir(path)
        else:
            os.mkdir(path)

        self._workspace_path = path

    def _make_workspace(self):
        ''' Make working directory '''
        return tempfile.mkdtemp(prefix="mcore_", dir='./')

    @property
    def workers(self):
        return self._num_processes

    @workers.setter
    def workers(self, n):
        assert not self._running, "Can't set workers if Manticore is running."
        self._num_processes = n

    @property
    def policy(self):
        return self._policy

    @policy.setter
    def policy(self, policy):
        assert not self._running, "Can't set policy if Manticore is running."
        self._policy = policy

    @property
    def coverage_file(self):
        return self._coverage_file

    @coverage_file.setter
    def coverage_file(self, path):
        assert not self._running, "Can't set coverage file if Manticore is running."
        self._coverage_file = path

    @property
    def memory_errors_file(self):
        return self._memory_errors

    @memory_errors_file.setter
    def memory_errors_file(self, path):
        assert not self._running, "Can't set memory errors if Manticore is running."
        self._memory_errors = path

    @property
    def env(self):
        return self._env

    @env.setter
    def env(self, env):
        '''
        Update environment variables from |env|. Use repeated '+' chars for
        symbolic values.
        '''
        assert isinstance(env, dict)
        assert not self._running, "Can't set process env if Manticore is running."

        self._env.update(env)
        return self._env

    def env_add(self, key, value, overwrite=True):
        if key in self._env:
            if overwrite:
                self._env[key] = value
        else:
            self._env[key] = value

    @property
    def arch(self):
        assert self._binary is not None

        if self._arch is not None:
            return self._arch

        arch = self._binary_obj.get_machine_arch()
        if   arch == 'x86': self._arch = 'i386'
        elif arch == 'x64': self._arch = 'x86_64'
        elif arch == 'ARM': self._arch = 'arm'
        else: raise "Unsupported architecture: %s"%(arch, )

        return self._arch
        

    def _start_workers(self, num_processes, profiling=False):
        assert num_processes > 0, "Must have more than 0 worker processes"

        logger.info("Starting %d processes.", num_processes)

        if profiling:
            profile = cProfile.Profile()
            def profile_this(func, *args, **kwargs):
                profile.enable()
                result = func(*args, **kwargs)
                profile.disable()
                profile.create_stats()
                self.profile_stats.append(_profile.stats.items())
                return result
            return func
            target = profile_this(self._executor.run)
        else:
            target = self._executor.run

        for _ in range(num_processes):
            p = Process(target=target, args=())
            self._workers.append(p)
            p.start()

    def _join_workers(self):
        while len(self._workers) > 0:
            w = self._workers.pop()
            try:
                w.join()
            except KeyboardInterrupt, e:
                self._executor.shutdown()
                # multiprocessing.dummy.Process does not support terminate
                if hasattr(w, 'terminate'):
                    w.terminate()
                self._workers.append(w)


    ############################################################################
    # Model hooks + callback
    ############################################################################

    def apply_model_hooks(self, path):
        #TODO(yan): Simplify the partial function application

        # Imported straight from __main__.py; this will be re-written once the new
        # event code is in place.
        import core.cpu
        import importlib
        import models

        with open(path, 'r') as fnames:
            for line in fnames.readlines():
                address, cc_name, name = line.strip().split(' ')
                cc = getattr(core.cpu.x86.ABI, cc_name)
                fmodel = models
                name_parts = name.split('.')
                importlib.import_module(".models.{}".format(name_parts[0]), 'manticore')
                for n in name_parts:
                    fmodel = getattr(fmodel,n)
                assert fmodel != models
                logger.debug("[+] Hooking 0x%x %s %s", int(address,0), cc_name, name )
                def cb_function(cc, fmodel, state):
                    cc(fmodel)(state.model)
                cb = functools.partial(cb_function, cc, fmodel)
                # TODO(yan) this should be a dict
                self._model_hooks.setdefault(int(address,0), set()).add(cb)

    def _model_hook_callback(self, state):
        pc = state.cpu.PC
        if pc not in self._model_hooks:
            return

        for cb in self._model_hooks[pc]:
            cb(state)


    ############################################################################
    # Assertion hooks + callback
    ############################################################################

    def load_assertions(self, path):
        with open(path, 'r') as f:
            for line in f.readlines():
                pc = int(line.split(' ')[0], 16)
                if pc in self._assertions:
                    logger.debug("Repeated PC in assertions file %s", path)
                self._assertions[pc] = ' '.join(line.split(' ')[1:])

    def _backup_state_callback(self, state, state_id):
        logger.debug("Backup state %r", state_id)

    def _restore_state_callback(self, state, state_id):
        logger.debug("Restore state %r", state_id)

    def _terminate_state_callback(self, state, state_id, ex):
        executor = self._executor
        #aggregates state statistics into exceutor statistics. FIXME split
        logger.debug("Terminate state %r %r ", state, state_id)
        state_visited = state.context.get('visited', set())
        state_instructions_count = state.context.get('instructions_count', 0)
        with self._executor.locked_context() as context:
            executor_visited = context.get('visited', set())
            context['visited'] = executor_visited.union(state_visited)

            executor_instructions_count = context.get('instructions_count', 0)
            context['instructions_count'] = executor_instructions_count + state_instructions_count 

    def _fork_state_callback(self, state, expression, values, policy):
        logger.debug("About to backup state %r %r %r", state, expression, values, policy)

    def _read_register_callback(self, state, cpu, reg_name, value):
        logger.debug("Read Register %r %r", reg_name, value)

    def _write_register_callback(self, state, cpu, reg_name, value):
        logger.debug("Write Register %r %r", reg_name, value)

    def _read_memory_callback(self, state, cpu, address, value, size):
        logger.debug("Read Memory %r %r %r", address, value, size)

    def _write_memory_callback(self, state, cpu, address, value, size):
        logger.debug("Write Memory %r %r %r", address, value, size)

    def _decode_instruction_callback(self, state, cpu):
        logger.debug("Decoding stuff instruction not available")

    def _execute_instruction_callback(self, state, cpu, instruction):
        address = state.cpu.PC
        if not issymbolic(address):
            state.context.setdefault('visited', set()).add(address)
            count = state.context.get('instructions_count', 0)
            state.context['instructions_count'] = count + 1


    def _generate_testcase_callback(self, state, testcase_id, message = 'Testcase generated'):
        #Fixme split this!
        '''
        Create a serialized description of a given state.
        :param state: The state to generate information about
        :param message: Accompanying message
        '''
        import StringIO
        _getFilename = self._executor._workspace_filename
        logger.debug("testcase",state, testcase_id, message)
        test_number = testcase_id

        logger.info("Generating testcase No. %d - %s",
                test_number, message)

        # Summarize state
        output = StringIO.StringIO()
        memories = set()

        output.write("Command line:\n  " + ' '.join(sys.argv) + '\n')
        output.write('Status:\n  {}\n'.format(message))
        output.write('\n')

        for cpu in filter(None, state.model.procs):
            idx = state.model.procs.index(cpu)
            output.write("================ PROC: %02d ================\n"%idx)

            output.write("Memory:\n")
            if hash(cpu.memory) not in memories:
                for m in str(cpu.memory).split('\n'):
                    output.write("  %s\n"%m)
                memories.add(hash(cpu.memory))

            output.write("CPU:\n{}".format(cpu))

            if hasattr(cpu, "instruction") and cpu.instruction is not None:
                i = cpu.instruction
                output.write("  Instruction: 0x%x\t(%s %s)\n" %(i.address, i.mnemonic, i.op_str))
            else:
                output.write("  Instruction: {symbolic}\n")

        with open(_getFilename('test_%08x.messages'%test_number),'a') as f:
            f.write(output.getvalue())
            output.close()

        tracefile = 'test_{:08x}.trace'.format(test_number)
        with open(_getFilename(tracefile), 'w') as f:
            for pc in state.context['visited']:
                f.write('0x{:08x}\n'.format(pc))

        # Save constraints formula
        smtfile = 'test_{:08x}.smt'.format(test_number)
        with open(_getFilename(smtfile), 'wb') as f:
            f.write(str(state.constraints))
        
        assert solver.check(state.constraints)
        for symbol in state.input_symbols:
            buf = solver.get_value(state.constraints, symbol)
            file(_getFilename('test_%08x.txt'%test_number),'a').write("%s: %s\n"%(symbol.name, repr(buf)))
        
        file(_getFilename('test_%08x.syscalls'%test_number),'a').write(repr(state.model.syscall_trace))

        stdout = ''
        stderr = ''
        for sysname, fd, data in state.model.syscall_trace:
            if sysname in ('_transmit', '_write') and fd == 1:
                stdout += ''.join(map(str, data))
            if sysname in ('_transmit', '_write') and fd == 2:
                stderr += ''.join(map(str, data))
        file(_getFilename('test_%08x.stdout'%test_number),'a').write(stdout)
        file(_getFilename('test_%08x.stderr'%test_number),'a').write(stderr)

        # Save STDIN solution
        stdin_file = 'test_{:08x}.stdin'.format(test_number)
        with open(_getFilename(stdin_file), 'wb') as f:
            try:
                for sysname, fd, data in state.model.syscall_trace:
                    if sysname not in ('_receive', '_read') or fd != 0:
                        continue
                    for c in data:
                        f.write(chr(solver.get_value(state.constraints, c)))
            except SolverException, e:
                f.seek(0)
                f.write("{SolverException}\n")
                f.truncate()

        return test_number


    def _dump_stats_callback(self):

        #Fixme this is duplicated?
        if self.coverage_file is not None:
            executor_visited = _shared_context.get('visited', set())

            with open(self.coverage_file, "w") as f:
                fmt = "0x{:016x}\n"
                for m in executor_visited:
                    f.write(fmt.format(m[1]))

        visited = ['%d:%08x'%(0,site) for site in self._executor._shared_context.get('visited', set())]
        with file(os.path.join(self.workspace,'visited.txt'),'w') as f:
            for entry in sorted(visited):
                f.write(entry + '\n')

                    
        #if self.memory_errors_file is not None:
        #    with open(self._args.errorfile, "w") as f:
        #        fmt = "0x{:016x}\n"
        #        for m in self._executor.errors:
        #            f.write(fmt.format(m))


        instructions_count = self._executor._shared_context.get('instructions_count',0)
        elapsed = time.time()-self._time_started
        logger.info('Results dumped in %s', self.workspace)
        logger.info('Instructions executed: %d', instructions_count)
        logger.info('Coverage: %d different instructions executed', len(self._executor._shared_context['visited']))
        #logger.info('Number of paths covered %r', State.state_count())
        logger.info('Total time: %s', elapsed)
        logger.info('IPS: %d', instructions_count/elapsed)


        with file(os.path.join(self.workspace,'command.sh'),'w') as f:
            f.write(' '.join(sys.argv))
        
    def run(self, timeout=0):
        '''
        Runs analysis.
        '''
        assert not self._running, "Manticore is already running."
        args = self._args

        replay=None
        if hasattr(args, 'replay') and args.replay is not None:
            with open(args.replay, 'r') as freplay:
                replay = map(lambda x: int(x, 16), freplay.readlines())

        initial_state = self._make_state(self._binary)

        self._executor = Executor(initial_state,
                                  workspace=self.workspace, 
                                  policy=self._policy, 
                                  dumpafter=self.dumpafter, 
                                  maxstates=self.maxstates,
                                  maxstorage=self.maxstorage,
                                  replay=replay,
                                  dumpstats=self.should_profile)
        


        #Link Executor events to default callbacks in manticore object
        self._executor.will_read_register += self._read_register_callback
        self._executor.will_write_register += self._write_register_callback
        self._executor.will_read_memory += self._read_memory_callback
        self._executor.will_write_memory += self._write_memory_callback
        self._executor.will_execute_instruction += self._execute_instruction_callback
        self._executor.will_decode_instruction += self._decode_instruction_callback
        self._executor.will_backup_state += self._backup_state_callback
        self._executor.will_restore_state += self._restore_state_callback
        self._executor.will_fork_state += self._fork_state_callback
        self._executor.will_terminate_state += self._terminate_state_callback
        self._executor.will_generate_testcase += self._generate_testcase_callback

        if self._hooks:
            self._executor.will_execute_state += self._hook_callback

        if self._model_hooks:
            self._executor.will_execute_state += self._model_hook_callback

        if self._assertions:
            self._executor.will_execute_state += self._assertions_callback

        self._time_started = time.time()

        self._running = True

        if timeout > 0:
            t = Timer(timeout, self.terminate)
            t.start()
        try:
            self._start_workers(self._num_processes, profiling=False)

            self._join_workers()
        finally:
            self._running = False
            if timeout > 0:
                t.cancel()

        if self.should_profile:

            class PstatsFormatted:
                def __init__(self, d):
                    self.stats = dict(d)
                def create_stats(self):
                    pass

            ps = None
            for item in self._stats:
                try:
                    stat = PstatsFormatted(item)
                    if ps is None:
                        ps = pstats.Stats(stat)
                    else:
                        ps.add(stat)
                except TypeError:
                    logger.debug("Incorrectly formatted profiling information in _stats, skipping")

            if ps is None:
                logger.info("Profiling failed")
            else:
                filename = self._getFilename('profiling.bin') 
                logger.info("Dumping profiling info at %s", filename)
                ps.dump_stats(filename)

            results = ProfilingResults(ps, self.count)

            logger.info("Total profiled time: %f", results.time_elapsed)
            logger.info("Loading state time: %f", results.loading_time)
            logger.info("Saving state time: %f", results.saving_time)
            logger.info("Solver time: %f", results.solver_time)
            logger.info("Other time: %f", results.time_elapsed - (results.loading_time + results.saving_time + results.solver_time))
            return results




        self._dump_stats_callback()

        logger.info('Results dumped in %s', self.workspace)
        #logger.info('Instructions executed: %d', self._executor.count)
        #logger.info('Coverage: %d different instructions executed', len(self._executor.visited))
        #logger.info('Number of paths covered %r', State.state_count())
        logger.info('Total time: %s', time.time()-self._time_started)
        #logger.info('IPS: %d', self._executor.count/(time.time()-self._time_started))

    def terminate(self):
        '''
        Gracefully terminate the currently-executing run. Typically called from within
        a :func:`~hook`.
        '''
        self._executor.shutdown()

    def _assertions_callback(self, state):
        pc = state.cpu.PC
        if pc not in self._assertions:
            return

        from core.parser import parse

        program = self._assertions[pc]

        #This will interpret the buffer specification written in INTEL ASM.
        # (It may dereference pointers)
        assertion = parse(program, state.cpu.read, state.cpu.read_register)
        if not solver.can_be_true(state.constraints, assertion):
            logger.info(str(state.cpu))
            logger.info("Assertion %x -> {%s} does not hold. Aborting state.",
                    state.cpu.pc, program)
            raise TerminateState()

        #Everything is good add it.
        state.constraints.add(assertion)

    def _hook_callback(self, state):
        pc = state.cpu.PC
        'Invoke all registered generic hooks'

        # Ignore symbolic pc.
        # TODO(yan): Should we ask the solver if any of the hooks are possible,
        # and execute those that are?
        if not isinstance(pc, (int, long)):
            return

        # Invoke all pc-specific hooks
        for cb in self._hooks.get(pc, []):
            cb(state)

        # Invoke all pc-agnostic hooks
        for cb in self._hooks.get(None, []):
            cb(state)


