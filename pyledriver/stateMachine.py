'''
Controls and manages state of the alarm system. The design follows a reactor-
like model (similar to twisted) except the statemachine itself is passive (eg it
does not run/block on its own thread waiting for events). Instead the
StateMachine object holds the current status as well as the logic to move
between states, while child threads (listeners) deliver signals to the state
machine in response to external events. The state machine itself does not "wait"
but rather its selectState method could be called within any child thread (hence
the lock) which also means the entry/exit functions of each state are run within
the signal-originating child thread.
'''
import RPi.GPIO as GPIO
import time, logging, enum, os
from threading import Lock, Event
from functools import partial
from collections import namedtuple

from exceptionThreading import ExceptionThread
from config import configFile, stateFile
from sensors import startDoorSensor, startMotionSensor
from gmail import intruderAlert
from listeners import KeypadListener, PipeListener
from blinkenLights import Blinkenlights
from soundLib import SoundLib
from webInterface import startWebInterface
from stream import Camera, FileDump

logger = logging.getLogger(__name__)

class _SIGNALS(enum.Enum):
	'''
	Valid signals the statemachine can recieve. The consequence of each signal
	depends on the current state
	'''
	ARM = enum.auto()
	INSTANT_ARM = enum.auto()
	LOCK = enum.auto()
	INSTANT_LOCK = enum.auto()
	DISARM = enum.auto()
	TIMOUT = enum.auto()
	TRIP = enum.auto()
	
class _CountdownTimer(ExceptionThread):
	'''
	Launches thread which self terminates after some time (given in seconds).
	Termination triggers some action (a function). Optionally, a sound can be
	assigned to each 'tick'
	'''
	def __init__(self, countdownSeconds, action, sound=None):
		self._stopper = Event()

		def countdown():
			for i in range(countdownSeconds, 0, -1):
				if self._stopper.isSet():
					return None
				if sound and i < countdownSeconds:
					sound.play()
				time.sleep(1)
			action()

		super().__init__(target=countdown, daemon=True)
		self.start()

	def stop(self):
		self._stopper.set()

	def __del__(self):
		self.stop()
		
def _resetUSBDevice(device):
	'''
	Resets a USB device using the de/reauthorization method. This is really
	crude but works beautifully
	'''
	devpath = os.path.join('/sys/bus/usb/devices/' + device + '/authorized')
	with open(devpath, 'w') as f:
		f.write('0')
	with open(devpath, 'w') as f:
		f.write('1')
	logger.debug('Reset USB device: %s', devpath)

class _State:
	'''
	Represents one discrete status of the system. Each state has a set of entry
	and exit functions and optionaly has sound that can play upon state entry.
	States link to other states via the addTransition function, which links
	another state with a signal. In this way, many states can be linked together
	in a network.
	
	There is currently nothing stopping multiple states from sharing the same
	name...try not to be an idiot. This mostly matters in equality tests, which
	only compares the name
	'''
	def __init__(self, name, entryCallbacks=[], exitCallbacks=[], sound=None):
		self.name = name
		self.entryCallbacks = entryCallbacks
		self.exitCallbacks = exitCallbacks
		self._transTbl = {}
		
		self._sound = sound
		
	def entry(self):
		logger.info('entering ' + self.name)
		if self._sound:
			self._sound.play()
		for c in self.entryCallbacks:
			c()
		
	def exit(self):
		logger.info('exiting ' + self.name)
		if self._sound:
			self._sound.stop()
		for c in self.exitCallbacks:
			c()

	def next(self, signal):
		if signal in _SIGNALS:
			return self if signal not in self._transTbl else self._transTbl[signal]
		else:
			raise Exception('Illegal signal')
			
	def addTransition(self, signal, state):
		self._transTbl[signal] = state
	
	def __str__(self):
		return self.name
	
	def __eq__(self, other):
		return self.name == other
		
class StateMachine:
	'''
	Manager for states. This is intended to be used as a context manager (eg
	"with" statement) for brevity...and because there should only be one.
	
	Init is responsible for setting up all objects (including child threads) as
	well as contructing the state network. Each thread functions as some kind of
	listener (or supports one) that wait for an event to happen.
	
	Note we distinguish between "managed" and "non-managed" objects; the former
	need to be started/stopped to ensure things get cleaned. Managed objects are
	added to the managed list with _addManaged, which also returns a ref to the 
	object for other uses.
	
	Upon opening the context, all threads are started. Note that not all threads
	are managed; some are started and forgotten, as these require no cleanup.
	Managed threads are started with _startManaged and stopped with _stopManaged
	upon closing the context. This system has the added benefit of not trying to
	stop an object that has not been initialized, as it cannot appear in the
	managed list otherwise
	
	During steady-state operation, the receiver for signals that make things
	happen is selectState, intended to be called from any of the state machine's
	child threads. This calls the current state's "next" method and sets the
	result as the new current state. Note the lock because it could be called by
	any number of listeners simultaneously
	'''
	def __init__(self):
		self._lock = Lock()
		self._managed = []
		
		self.soundLib = self._addManaged(SoundLib())
		self.fileDump = self._addManaged(FileDump())
		
		self._addManaged(Camera())
		
		# add signals to self to avoid calling partial every time
		for sig in _SIGNALS:
			setattr(self, sig.name, partial(self.selectState, sig))

		secretTable = {secret: getattr(self, signal) for signal, secret in configFile['secretTable'].items()}
		
		def secretCallback(secret, logger):
			if secret in secretTable:
				secretTable[secret]()
				logger.debug('Secret pipe listener received: \"%s\"', secret)
			elif logger:
				logger.debug('Secret pipe listener received invalid secret')
	
		self._addManaged(PipeListener(callback=secretCallback, name='secret'))

		self._addManaged(KeypadListener(stateMachine=self, passwd=configFile['keyPasswd']))
		
		def startTimer(t, sound):
			self._timer = _CountdownTimer(t, self.TIMOUT, sound)
			
		def stopTimer():
			if self._timer.is_alive():
				self._timer.stop()
				self._timer = None
				
		sfx = self.soundLib.soundEffects
				
		LED = self._addManaged(Blinkenlights(17))
		
		def squareBlink(t):
			LED.setBlink(True)
			LED.setTriangle(False)
			LED.setCyclePeriod(t)
			
		def triangleBlink(t):
			LED.setBlink(True)
			LED.setTriangle(True)
			LED.setCyclePeriod(t)
			
		stateObjs = [
			_State(
				name = 'disarmed',
				entryCallbacks = [partial(LED.setBlink, False)],
				sound = sfx['disarmed']
			),
			_State(
				name = 'armedCountdown',
				entryCallbacks = [partial(squareBlink, 1), partial(startTimer, 30, sfx['armedCountdown'])],
				exitCallbacks = [stopTimer],
				sound = sfx['armedCountdown']
			),
			_State(
				name = 'armed',
				entryCallbacks = [partial(triangleBlink, 2)],
				sound = sfx['armed']
			),
			_State(
				name = 'lockedCountdown',
				entryCallbacks = [partial(squareBlink, 1), partial(startTimer, 30, sfx['lockedCountdown'])],
				exitCallbacks = [stopTimer],
				sound = sfx['lockedCountdown']
			),
			_State(
				name = 'locked',
				entryCallbacks = [partial(squareBlink, 2)],
				sound = sfx['locked']
			),
			_State(
				name = 'trippedCountdown',
				entryCallbacks = [partial(squareBlink, 1), partial(startTimer, 30, sfx['trippedCountdown'])],
				exitCallbacks = [stopTimer],
				sound = sfx['trippedCountdown']
			),
			_State(
				name = 'tripped',
				entryCallbacks = [partial(triangleBlink, 1), intruderAlert],
				sound = sfx['tripped']
			)
		]
		
		self.states = st = namedtuple('States', [obj.name for obj in stateObjs])(*stateObjs)

		st.disarmed.addTransition(			_SIGNALS.ARM, 			st.armedCountdown)
		st.disarmed.addTransition(			_SIGNALS.INSTANT_ARM, 	st.armed)
		st.disarmed.addTransition(			_SIGNALS.LOCK, 			st.lockedCountdown)
		st.disarmed.addTransition(			_SIGNALS.INSTANT_LOCK, 	st.locked)
		
		st.armedCountdown.addTransition(	_SIGNALS.DISARM, 		st.disarmed)
		st.armedCountdown.addTransition(	_SIGNALS.TIMOUT, 		st.armed)
		st.armedCountdown.addTransition(	_SIGNALS.INSTANT_ARM, 	st.armed)
		st.armedCountdown.addTransition(	_SIGNALS.LOCK, 			st.lockedCountdown)
		st.armedCountdown.addTransition(	_SIGNALS.INSTANT_LOCK, 	st.locked)
		
		st.armed.addTransition(				_SIGNALS.DISARM, 		st.disarmed)
		st.armed.addTransition(				_SIGNALS.TRIP, 			st.trippedCountdown)
		st.armed.addTransition(				_SIGNALS.LOCK, 			st.lockedCountdown)
		st.armed.addTransition(				_SIGNALS.INSTANT_LOCK,	st.locked)
		
		st.lockedCountdown.addTransition(	_SIGNALS.DISARM, 		st.disarmed)
		st.lockedCountdown.addTransition(	_SIGNALS.TIMOUT, 		st.locked)
		st.lockedCountdown.addTransition(	_SIGNALS.INSTANT_LOCK, 	st.locked)
		st.lockedCountdown.addTransition(	_SIGNALS.ARM, 			st.armedCountdown)
		st.lockedCountdown.addTransition(	_SIGNALS.INSTANT_ARM, 	st.armed)
		
		st.locked.addTransition(			_SIGNALS.DISARM, 		st.disarmed)
		st.locked.addTransition(			_SIGNALS.TRIP, 			st.trippedCountdown)
		st.locked.addTransition(			_SIGNALS.ARM, 			st.armedCountdown)
		st.locked.addTransition(			_SIGNALS.INSTANT_ARM, 	st.armed)
		
		st.trippedCountdown.addTransition(	_SIGNALS.DISARM, 		st.disarmed)
		st.trippedCountdown.addTransition(	_SIGNALS.TIMOUT, 		st.tripped)
		st.trippedCountdown.addTransition(	_SIGNALS.ARM, 			st.armed)
		st.trippedCountdown.addTransition(	_SIGNALS.INSTANT_ARM, 	st.armed)
		st.trippedCountdown.addTransition(	_SIGNALS.LOCK, 			st.locked)
		st.trippedCountdown.addTransition(	_SIGNALS.INSTANT_LOCK,	st.locked)
		
		st.tripped.addTransition(			_SIGNALS.DISARM, 		st.disarmed)
		st.tripped.addTransition(			_SIGNALS.ARM, 			st.armed)
		st.tripped.addTransition(			_SIGNALS.INSTANT_ARM, 	st.armed)
		st.tripped.addTransition(			_SIGNALS.LOCK, 			st.locked)
		st.tripped.addTransition(			_SIGNALS.INSTANT_LOCK,	st.locked)
		
		self.currentState = getattr(self.states, stateFile['state'])
		
	def __enter__(self):
		_resetUSBDevice('1-1')
		
		# start all managed threads (we retain ref to these to stop them later)
		self._startManaged()
		
		activeSensorStates = (self.states.armed, self.states.trippedCountdown, self.states.tripped)
		
		def sensorAction(location, logger):
			cst = self.currentState
			level = logging.INFO if cst in activeSensorStates else logging.DEBUG
			logger.log(level, 'detected motion: ' + location)
			if cst == self.states.armed:
				self.selectState(_SIGNALS.TRIP)

		def videoAction(location, logger, pin):
			sensorAction(location, logger)
			cst = self.currentState
			if cst in activeSensorStates:
				self.fileDump.addInitiator(pin)
				while GPIO.input(pin) and cst in activeSensorStates:
					time.sleep(0.1)
				self.fileDump.removeInitiator(pin)
				
		activeDoorStates = activeSensorStates + (self.states.locked,)

		def doorAction(closed, logger):
			self.soundLib.soundEffects['door'].play()
			cst = self.currentState
			level = logging.INFO if cst in activeDoorStates else logging.DEBUG
			entry = 'door closed' if closed else 'door opened'
			logger.log(level, entry)
			if not closed and cst == self.states.armed or cst == self.states.locked:
				self.selectState(_SIGNALS.TRIP)

		# start non-managed threads (we forget about these because they can exit with no cleanup)
		startMotionSensor(5, 'Nate\'s room', sensorAction)
		startMotionSensor(19, 'front door', sensorAction)
		startMotionSensor(26, 'Laura\'s room', sensorAction)
		
		startMotionSensor(6, 'deck window', partial(videoAction, pin=6))
		startMotionSensor(13, 'kitchen bar', partial(videoAction, pin=13))
		
		startDoorSensor(22, doorAction)
		
		startWebInterface(self)
		
		self.currentState.entry()

	def __exit__(self, exception_type, exception_value, traceback):
		self._stopManaged()

	def selectState(self, signal):
		with self._lock:
			nextState = self.currentState.next(signal)
			if nextState != self.currentState:
				self.currentState.exit()
				self.currentState = nextState
				self.currentState.entry()
			
			stateFile['state'] = self.currentState.name
			
	def _addManaged(self, obj):
		self._managed.append(obj)
		return obj

	def _startManaged(self):
		for m in self._managed:
			m.start()
	
	def _stopManaged(self):
		for m in self._managed:
			m.stop()
