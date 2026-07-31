import sounddevice as sd
import soundfile as sf
data, fs = sf.read('/tmp/openpal_tx.wav')
sd.play(data, fs,device=1)
sd.wait()
print('Done')
