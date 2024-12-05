import os
import tempfile
import pickle, codecs	# load / store
import mxw, mxw_imgui	# for mxw interaction, mxw ui interaction
import cv2 				# image processing
import numpy as np		# math

# video writer to do:
# - limit preload index to actually existing preload count
# - allow keep video and user-storage path

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

		# reference media (will start capturing)
		m = mxw.media(v.capture_device)
		if(m.isvalid()):
			m.reference(True)

	else:
		# create tempfile
		v.f = tempfile.NamedTemporaryFile(prefix=temp_file_prefix, suffix='.mp4')
		v.f.close()
		v.out = cv2.VideoWriter(v.f.name, fourcc, mxw.fps, v.videosize)

		# reference media (will start capturing)
		m = mxw.media(v.capture_device)
		if(m.isvalid()):
			m.reference(True)

	return

def onPostAction():
	v = instance_storage[item_id]

	# release writer (close file)
	if hasattr(v, 'out'):
		v.out.release()
		del v.out;

	# refcount capture device
	m = mxw.media(v.capture_device)
	if(m.isvalid()):
		m.reference(False)

	# load into preload if requested
	if(v.load_into_preload_after_record):
		if v.file_option == 1:
			mxw.preload(v.preload_index).set_media(v.file_path)
		else:
			mxw.preload(v.preload_index).set_media(v.f.name)
			del v.f
	return

def onNewFrameInPlayoutCue():
	v = instance_storage[item_id]
	m = mxw.media(v.capture_device)
	if(m.isvalid()):
		img = m.get_image_sample_cvmat(v.videosize[0],v.videosize[1])
		img = np.array(img, copy=False)
		img = cv2.flip(img, 0)
		v.out.write(img)
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
