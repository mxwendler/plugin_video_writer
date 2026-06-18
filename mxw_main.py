import os
import tempfile
import threading, queue	# off-thread encoding
import pickle, codecs	# load / store
import mxw, mxw_imgui	# for mxw interaction, mxw ui interaction
import cv2 				# image processing
import numpy as np		# math

# video writer to do:
# - limit preload index to actually existing preload count
# - allow keep video and user-storage path

# bounded encode queue: caps memory if the encoder falls behind. on overflow
# frames are dropped (counted) rather than stalling the render thread.
FRAME_QUEUE_MAX = 120

# example per-instance-storage: create a dictionary
# and use it with 'item_id' as key (this integer is set by host application before every function call)
class video_writer:
	capture_device=""
	videosize = [1280,720]
	preload_index = 1
	load_into_preload_after_record = True

	# Add radio button option for choosing between a temporary file and a specified file
	file_option = 0
	file_path = ""

	# runtime-only members (writer, worker thread, queue, cached handle, pacing)
	# must not be serialized - strip them before pickling
	_runtime_fields = (
		'out', 'f', 'q', 'worker', 'err', 'dropped',
		'media', 'record_start_millis', 'frames_written')

	def __getstate__(self):
		state = self.__dict__.copy()
		for k in self._runtime_fields:
			state.pop(k, None)
		return state
instance_storage = {}

# ------------------------ temp file management -------------------------------------
temp_file_prefix="mxw_video_writer_temp_"
def delete_temp_video_files():
    # Get the path to the temporary directory
    # Loop over all files in the temp directory
    temp_dir = tempfile.gettempdir()
    for filename in os.listdir(temp_dir):
        if filename.startswith(temp_file_prefix) and filename.endswith('.mp4'):
            file_path = os.path.join(temp_dir, filename)
            try:
                os.remove(file_path)
                mxw.print_console(f"Plugin video writer: deleted {file_path}")
            except Exception as e:
                mxw.print_console(f"Plugin video writer: failed to delete {file_path} / {e}")

# Call the function to delete matching files
delete_temp_video_files()

# ------------------------ off-thread encoder ---------------------------------------
# the worker owns its queue and writer (passed as locals, NOT read from v) so a
# worker draining a previous recording can never touch the next recording's
# state. OpenCV releases the GIL inside write(), so the encode runs concurrently
# with the render thread. it never calls mxw.* (not thread-safe) - errors are
# stashed on v.err and surfaced from the render thread. the writer is released
# here, after the last frame, so stop never races a release on the main thread.
def _encode_loop(v, out, q):
	while True:
		frame = q.get()
		if frame is None:		# sentinel = stop
			break
		try:
			out.write(frame)
		except Exception as e:
			v.err = str(e)
	try:
		out.release()			# close the file only after the last frame is written
	except Exception:
		pass

# stop the worker and let it flush + release the writer itself. safe to call
# repeatedly and when not recording (onPostAction / onCleanup / onDelete).
def _stop_recording(v, drain_timeout=60.0):
	if hasattr(v, 'q'):
		try:
			v.q.put(None)					# sentinel, behind any backlog
		except Exception:
			pass
	if hasattr(v, 'worker'):
		try:
			v.worker.join(timeout=drain_timeout)	# worker owns + releases the writer
		except Exception:
			pass
	# drop our references; the worker thread owns the writer until it exits
	for a in ('out', 'q', 'worker'):
		if hasattr(v, a):
			delattr(v, a)

# finish a recording: stop the worker, unref the capture device, optionally load
# the result into preload, and clear runtime state. used by onPostAction and by
# the onNewFrameAlways backstop (a loop wrapping back never delivers post_action
# to a later cue, so the item must stop itself). load_preload is false on a
# restart, where the preload is about to be overwritten anyway.
def _finalize_recording(v, load_preload=True):
	# only act if a recording is in progress
	if not hasattr(v, 'out'):
		return

	# diagnostic: frames_written==0 here points at the capture device not
	# delivering frames (graph restart), not the encoder
	mxw.print_console(
		f"Plugin video writer: stop - frames_written={getattr(v, 'frames_written', 0)} "
		f"dropped={getattr(v, 'dropped', 0)}")

	# stop the worker; it flushes the queue and releases the writer itself
	_stop_recording(v)

	# unref the capture device (cached handle if present)
	m = getattr(v, 'media', None)
	if m is None:
		m = mxw.media(v.capture_device)
	if m.isvalid():
		m.reference(False)

	# load the result into preload
	if load_preload and v.load_into_preload_after_record:
		if v.file_option == 1:
			mxw.preload(v.preload_index).set_media(v.file_path)
		elif hasattr(v, 'f'):
			mxw.preload(v.preload_index).set_media(v.f.name)

	# drop remaining runtime handles
	for attr in ('media', 'err', 'dropped', 'record_start_millis', 'frames_written', 'f'):
		if hasattr(v, attr):
			delattr(v, attr)

# -----------------------------------------------------------------------------------
def onCreate():
	v = video_writer()
	dev = mxw.media().get_capture_device_names()
	v.capture_device = dev[1]
	instance_storage[item_id] = v
	return

# save and load: you can serialize into a string
def onSave():
	serialized = codecs.encode(pickle.dumps(instance_storage[item_id]), "base64").decode()
	return serialized

def onLoad( serialized ):
    instance_storage[item_id] = pickle.loads(codecs.decode(serialized.encode(), "base64"))
    return

def onAction():
	v = instance_storage[item_id]

	# safety: if a previous recording is still running (e.g. a loop wrapped back
	# to us without delivering post_action) stop it before starting a new one
	if hasattr(v, 'out'):
		_finalize_recording(v, load_preload=False)

	# clear preload
	if(v.load_into_preload_after_record):
		mxw.preload(v.preload_index).set_media("null")

	# set fourcc
	fourcc = cv2.VideoWriter_fourcc('M','P','4','V')

	# create video writer. either delete file from previous run or new temp file
	if v.file_option == 1:
		# first unload but may be first run
		if not	mxw.media(v.file_path).unload_media_full_if_not_used_by_clips():
			mxw.print_console(f"Plugin video writer: cannot unload {v.file_path}, maybe not in use")

		# os delete file
		if os.path.isfile(v.file_path):
			os.remove(v.file_path)
		else:
			mxw.print_console(f"Plugin video writer: cannot remove {v.file_path}, maybe still in use")

		# if clear, create writer
		if not os.path.isfile(v.file_path):
			v.out = cv2.VideoWriter(v.file_path, fourcc, mxw.fps, v.videosize)

	else:
		# create tempfile
		v.f = tempfile.NamedTemporaryFile(prefix=temp_file_prefix, suffix='.mp4')
		v.f.close()
		v.out = cv2.VideoWriter(v.f.name, fourcc, mxw.fps, v.videosize)

	# guard: if the writer could not be created/opened (e.g. file still in use)
	# bail out cleanly. the hot path checks hasattr(v,'out'), so recording simply
	# stays off instead of throwing every frame (which would disable the hook).
	if not hasattr(v, 'out') or not v.out.isOpened():
		mxw.print_console("Plugin video writer: could not open video writer, recording disabled")
		if hasattr(v, 'out'):
			del v.out
		return

	# resolve the capture device once and start it (instead of re-resolving per frame)
	v.media = mxw.media(v.capture_device)
	if(v.media.isvalid()):
		v.media.reference(True)

	# wall-clock pacing state: keep the file's timebase matching real time
	v.record_start_millis = mxw.millis
	v.frames_written = 0

	# start the encoder worker. pass writer + queue as args so the worker is
	# self-contained and a previous worker can never touch this recording.
	v.err = None
	v.dropped = 0
	v.q = queue.Queue(maxsize=FRAME_QUEUE_MAX)
	v.worker = threading.Thread(target=_encode_loop, args=(v, v.out, v.q), daemon=True)
	v.worker.start()

	# diagnostic: reveals device/writer state at start (helps catch e.g. a
	# capture device that did not restart on a second recording)
	mxw.print_console(
		f"Plugin video writer: start - device='{v.capture_device}' "
		f"valid={v.media.isvalid()} writer_open={v.out.isOpened()} "
		f"fps={mxw.fps} size={v.videosize}")

	return

def onPostAction():
	# normal stop: playback left our cue into a later one
	v = instance_storage[item_id]
	_finalize_recording(v, load_preload=True)
	return

def onNewFrameAlways():
	# backstop for loop wrap-around: the host only delivers post_action to cues
	# *before* the active one, so a recording at a later cue is never told to
	# stop when the playlist loops back. detect "recording but no longer the
	# active cue" here (this hook runs every frame regardless of active cue) and
	# finalize - placing the result into preload, just like a normal stop.
	v = instance_storage.get(item_id)
	if v is None or not hasattr(v, 'out'):
		return
	try:
		my_row = item_position[1]
		active = mxw.playlist.get_active_cue()
	except Exception:
		return
	# is_on_active_cue(): position.y == active_cue_index - 1
	if active != my_row + 1:
		_finalize_recording(v, load_preload=True)
	return

def onCleanup():
	# safety net: make sure a worker is never left running
	v = instance_storage.get(item_id)
	if v is not None:
		_finalize_recording(v, load_preload=False)
	return

def onDelete():
	# item destroyed: stop worker and forget instance
	v = instance_storage.pop(item_id, None)
	if v is not None:
		_finalize_recording(v, load_preload=False)
	return

def onNewFrameInPlayoutCue():
	v = instance_storage[item_id]

	# not recording (writer absent / failed to open): nothing to do
	if not hasattr(v, 'out') or not hasattr(v, 'q'):
		return

	# cached capture handle
	m = getattr(v, 'media', None)
	if m is None or not m.isvalid():
		return

	# async (non-stalling) grab, already flipped top-down by the host
	img = m.get_image_sample_cvmat_async(v.videosize[0], v.videosize[1])
	arr = np.array(img, copy=False)
	if arr.size == 0:
		return

	# wall-clock pacing: how many frames the file should hold by now
	fps = max(1, int(mxw.fps))
	elapsed_ms = mxw.millis - v.record_start_millis
	target = int(elapsed_ms * fps / 1000)

	# rendering faster than fps: nothing new to write yet
	if v.frames_written >= target:
		# still surface any pending encoder error
		if v.err:
			mxw.print_console("Plugin video writer: " + v.err)
			v.err = None
		return

	# one owning copy (the async buffer is reused next frame); enqueue it,
	# duplicating to fill gaps when rendering fell behind fps. cap the burst
	# to one second so a long stall cannot flood the queue.
	frame = arr.copy()
	catch_up = min(target - v.frames_written, fps)
	for _ in range(catch_up):
		try:
			v.q.put_nowait(frame)
			v.frames_written += 1
		except queue.Full:
			v.dropped += 1
			break

	# surface encoder errors from the worker thread (render thread only)
	if v.err:
		mxw.print_console("Plugin video writer: " + v.err)
		v.err = None
	return

def renderBlinking():
	v = instance_storage[item_id]
	return hasattr(v, 'out')

def limit_and_round_to_multiple_of_4(num):
    num = max(320, min(num, 4096))    # Ensure the number is within the range of 320 and 4096
    num = (num // 4) * 4  # This rounds down to the nearest multiple of 4
    return num

# render in panel for settings etc
def onRenderPanel():
	v = instance_storage[item_id]

	# explanation
	mxw_imgui.text_unformatted("This plugin records a camera stream")

	# action state
	if(hasattr(v,'f')):
		mxw_imgui.text_unformatted(v.f.name)
	if(hasattr(v,'out') and v.out.isOpened()):
		mxw_imgui.text_unformatted("Recording")
		if(getattr(v, 'dropped', 0)):
			mxw_imgui.text_unformatted(f"Dropped frames: {v.dropped}")
	else:
		mxw_imgui.text_unformatted("Not recording")

	# device names
	dev = mxw.media().get_capture_device_names()
	a = mxw_imgui.combo("Capture Device", dev.index(v.capture_device), dev)
	if(a[0]):
		print(str(a[1]))
		v.capture_device = dev[a[1]]

	# recording resolution
	b = mxw_imgui.drag_int2("Recording resolution", v.videosize)
	if(b[0]):
		v.videosize = b[1]
		v.videosize[0] = limit_and_round_to_multiple_of_4(v.videosize[0])
		v.videosize[1] = limit_and_round_to_multiple_of_4(v.videosize[1])

	c = mxw_imgui.checkbox("Load into preload after recording", v.load_into_preload_after_record)
	if(c[0]):
		v.load_into_preload_after_record = c[1]

	d = mxw_imgui.drag_int("Target preload index", v.preload_index, 10, 1, 1000)
	if(d[0]):
		v.preload_index = d[1]

	# Add radio buttons for file selection
	mxw_imgui.text_unformatted("File Storage Option:")
	changed = False
	if mxw_imgui.radio_button("Temporary File", v.file_option == 0):
		v.file_option = 0
		changed = True
	if mxw_imgui.radio_button("Specified File", v.file_option == 1):
		v.file_option = 1
		changed = True

	# If specified file is chosen, show input box for file path
	if v.file_option == 1:
		f = mxw_imgui.input_text("File Path", v.file_path, 256)
		if f[0]:
			v.file_path = f[1]

	return
