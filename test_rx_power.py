#!/usr/bin/env python3
"""GR flowgraph loopback power test."""
import numpy as np
from gnuradio import gr, blocks, analog, uhd

class PowerProbe(blocks.head):
    """Measure power of first N complex samples."""
    def __init__(self, n):
        blocks.head.__init__(self, gr.sizeof_gr_complex, n)
        self._buf = []

    def work(self, input_items, output_items):
        self._buf.append(input_items[0].copy())
        return len(input_items[0])

    def get_power(self):
        if not self._buf:
            return 0
        data = np.concatenate(self._buf)
        return float(np.mean(np.abs(data)**2)), float(np.max(np.abs(data))), len(data)

# Build flowgraph
tb = gr.top_block("Loopback Test")

dev = "serial=34D628A"
sr = 1e6
n_samps = int(3e6)

# TX: continuous tone
tone = analog.sig_source_c(sr, analog.GR_COS_WAVE, 100e3, 0.5, 0)
tx_sink = uhd.usrp_sink(dev, uhd.stream_args("fc32"))
tx_sink.set_center_freq(915e6, 0)
tx_sink.set_gain(50, 0)
tx_sink.set_samp_rate(sr)

# RX: capture samples
rx_src = uhd.usrp_source(dev, uhd.stream_args("fc32"))
rx_src.set_center_freq(915e6, 0)
rx_src.set_gain(50, 0)
rx_src.set_samp_rate(sr)

# Measure RX power
probe = blocks.head(gr.sizeof_gr_complex, n_samps)
file_sink = blocks.file_sink(gr.sizeof_gr_complex, "/tmp/rx_test.dat", False)

tb.connect(tone, tx_sink)
tb.connect(rx_src, probe, file_sink)

print("Starting flowgraph...")
tb.start()
print("Running for 4 seconds...")
import time; time.sleep(4)
tb.stop()
tb.wait()

# Read back
data = np.fromfile("/tmp/rx_test.dat", dtype=np.complex64)
print(f'RX samples: {len(data)}')
if len(data) > 0:
    pwr = np.mean(np.abs(data)**2)
    print(f'Mean power: {pwr:.6f}')
    print(f'Peak amp: {np.max(np.abs(data)):.4f}')
    print(f'Mean amp: {np.mean(np.abs(data)):.4f}')

    # FFT
    fft = np.fft.fftshift(np.fft.fft(data))
    mag = np.abs(fft)**2
    pk = np.argmax(mag)
    freq = (pk - len(fft)//2) * sr / len(fft)
    snr = 10*np.log10(mag[pk]/max(np.median(mag), 1e-30))
    print(f'FFT peak offset: {freq:.0f} Hz (expect +100k)')
    print(f'Tone SNR: {snr:.1f} dB')
else:
    print('NO RX DATA')
