#/usr/bin/python2
import pycuda.driver as cuda
import pycuda.compiler
import pycuda.autoinit
import numpy as np
from itertools import chain, izip
from timecube import make_spacecube
from spaleta_error import phase_fit_error
# debugging imports
import pdb
import matplotlib.pyplot as plt
# TODO: add second moment based error calculations
# TODO: fix env model..
# TODO: create unit tests to compare SNR

C = 299792458.
FWHM_TO_SIGMA = 2.355 # conversion of fwhm to std deviation, assuming gaussian
LAMBDA_FIT = 1
SIGMA_FIT = 2

mod = pycuda.compiler.SourceModule("""
#include <stdio.h>
#include <stdint.h>

#define REAL 0
#define IMAG 1
#define MAX_SAMPLES 25 
#define MAX_ALPHAS 512 // MUST BE A POWER OF 2
#define MAX_FREQS 512 // MUST BE A POWER OF 2
#define PI (3.141592)
#define SPOT_WIDTH 3

typedef struct 
{
    float freq, alf, amp;
} peak;

__device__ peak calc_peak(int32_t peakidx, int32_t freqidx, int32_t alfidx, int32_t nalfs, int32_t nlags, int32_t nfreqs, double *P_f, float *freqs, float *alfs, float *ce_matrix, float *se_matrix, int32_t *lagmask, float *s_times, float *samples, float env_model);
__device__ float calc_amp(float alf, float env_model, int32_t alfidx, int32_t freqidx, float *ce_matrix, float *se_matrix,  int32_t *lagmask, float *s_times, float *samples, int32_t nlags, int32_t nfreqs);

// see generalizing the lomb-scargle periodogram, g. bretthorst
__global__ void calc_bayes(float *samples, int32_t *lags, float *alphas, float *lag_times, float *ce_matrix, float *se_matrix, double *P_f, float env_model, int32_t nsamples, int32_t nalphas, int32_t *n_good_lags_v)
{
    int32_t t, i, sample_offset, samplebase;
    double dbar2 = 0;
    double hbar2 = 0;
    int32_t n_good_samples = 0;
    float alpha;

    __shared__ float s_samples[MAX_SAMPLES * 2];
    __shared__ int32_t s_lags[MAX_SAMPLES];
    __shared__ float s_cs_f[MAX_ALPHAS];

     // parallel cache lag mask in shared memory
    samplebase = blockIdx.x * nsamples; 
    for(i = 0; i < nsamples / blockDim.x + 1; i++) {
        sample_offset = threadIdx.x + i * blockDim.x;
        if(sample_offset < nsamples) {
            s_lags[sample_offset] = (lags[samplebase + sample_offset] != 0);
        }
    }
    __syncthreads(); 

    // parallel cache samples in shared memory, each thread loading sample number tidx.x + n * nfreqs
    // mask out bad lags with zero
    samplebase = blockIdx.x * nsamples * 2; 
    for(i = 0; i < 2 * nsamples / blockDim.x + 1; i++) {
        sample_offset = threadIdx.x + i * blockDim.x;
        if(sample_offset < nsamples * 2) {
            s_samples[sample_offset] = samples[samplebase + sample_offset] * (s_lags[sample_offset >> 1] != 0);
        }
    }
    __syncthreads(); 
    
    // calculate number of *good* lags.. needed for dbar2 scaling
    for(i = 0; i < nsamples; i++) {
        if(s_lags[i]) {
            n_good_samples++;
        }
    }
    
    // parallel calculate cs_f given bad lags, cache in shared memory and store in global memory for later use (assumes nfreqs >= nalphas!!)
    // fastest if nfreqs == nalphas, avoids divering program counters between threads
    // TODO: add support for different environment models (e.g. sigma fit)
 
    if(threadIdx.x < nalphas) {
        s_cs_f[threadIdx.x] = 0;
        alpha = alphas[threadIdx.x];
        for(i = 0; i < nsamples; i++) {
            s_cs_f[threadIdx.x] += pow(exp(pow(-alpha * lag_times[i], env_model)),2) * (s_lags[i] != 0);
        }
    }
    __syncthreads(); 

    // calculate dbar2 
    for(i = 0; i < 2*nsamples; i+=2) {
        dbar2 += (pow(s_samples[i + REAL],2) + pow(s_samples[i + IMAG],2)) * s_lags[i >> 1];
    }
    dbar2 /= 2 * n_good_samples;
    __syncthreads(); 
    

    // RI[pulse][alpha][freq]
    // CS[alpha][time][freq]
    for(i =  0; i < nalphas; i++) {
        int32_t RI_offset = (blockIdx.x * blockDim.x * nalphas) + (i * blockDim.x) + threadIdx.x;
        float r_f = 0;
        float i_f = 0;

        
        for(t = 0; t < nsamples; t++) {
            int32_t CS_offset = (i * blockDim.x * nsamples) + (t * blockDim.x) + threadIdx.x;
            sample_offset = 2*t;

            r_f += s_samples[sample_offset + REAL] * ce_matrix[CS_offset] + \
                   s_samples[sample_offset + IMAG] * se_matrix[CS_offset];
            i_f += s_samples[sample_offset + REAL] * se_matrix[CS_offset] - \
                   s_samples[sample_offset + IMAG] * ce_matrix[CS_offset];
        }

        hbar2 = ((pow(r_f, 2) / s_cs_f[i]) + (pow(i_f, 2) / s_cs_f[i]));
        P_f[RI_offset] = log10(n_good_samples * 2 * dbar2 - hbar2) * (1 - ((double) n_good_samples)) - log10(s_cs_f[i]);
    }

    if(threadIdx.x == 0) {
        n_good_lags_v[blockIdx.x] = n_good_samples;
    }
}

// P_f is [pulse][alpha][freq]
// thread for each freq, block across pulses
// TODO: currently assumes a power of 2 number of freqs 
__global__ void find_peaks(double *P_f, int32_t *peaks, int32_t nalphas)
{
    int32_t i;
    __shared__ int32_t maxidx[MAX_FREQS];
    __shared__ double maxval[MAX_FREQS];

    maxidx[threadIdx.x] = 0;
    maxval[threadIdx.x] = -1e6;
    
    // find max along frequency axis
    for(i = 0; i < nalphas; i++) {
        int32_t P_f_idx = (blockIdx.x * blockDim.x * nalphas) + (i * blockDim.x) + threadIdx.x;

        if (P_f[P_f_idx] > maxval[threadIdx.x]) { 
            maxidx[threadIdx.x] = P_f_idx;
            maxval[threadIdx.x] = P_f[P_f_idx];
        }
    }

    __syncthreads();
    // parallel reduce maximum
    for(i = blockDim.x/2; i > 0; i >>=1) {
        if(threadIdx.x < i) {
           if(maxval[threadIdx.x + i] > maxval[threadIdx.x]) {
              maxval[threadIdx.x] = maxval[threadIdx.x + i];
              maxidx[threadIdx.x] = maxidx[threadIdx.x + i];
           }
        }
        __syncthreads();
    }

    if(threadIdx.x == 0) {
        peaks[blockIdx.x] = maxidx[threadIdx.x];
    }
}

// thread for each pulse, find fwhm and calculate ampltitude
__global__ void process_peaks(float *samples, float *ce_matrix, float *se_matrix, float *lag_times, float *freqs, float *alfs, double *P_f, float *snr, float *snr_peak, int32_t *lagmask, int32_t *n_good_lags, int32_t *peaks, float env_model, int32_t nfreqs, int32_t nalphas, int32_t nlags, int32_t *alphafwhm, int32_t *freqfwhm, double *amplitudes) 
{
    int32_t peakidx = peaks[threadIdx.x];
    int32_t i;
    float fitpwr = 0;
    float rempwr = 0;
    double apex = P_f[peakidx];

    float peakamp;
    float peakfreq;
    float peakalf;

    float fitted_signal[MAX_SAMPLES];
    float factor = (apex - .30103); // -.30103 is log10(.5)
     
    int32_t ffwhm = 1;
    int32_t afwhm = 1;
     
    int32_t pulse_lowerbound = peakidx - (peakidx % (nfreqs * nalphas));
    int32_t pulse_upperbound = pulse_lowerbound + (nfreqs * nalphas);

    peak p;

    __shared__ float s_times[MAX_SAMPLES]; 
    
    
    for(i = 0; i <= nlags / blockDim.x; i++) {
        int32_t idx = i * blockDim.x + threadIdx.x;
        if(idx < nlags) {
            s_times[idx] = lag_times[idx];
        }
    }
    __syncthreads();  

    // find alpha fwhm 
    for(i = peakidx; i < pulse_upperbound && P_f[i] > factor; i+=nfreqs) {
        afwhm++; 
    } 
    __syncthreads();  

    for(i = peakidx; i >= pulse_lowerbound && P_f[i] > factor; i-=nfreqs) {
        afwhm++; 
    }
    __syncthreads();  

    // find freq fwhm
    // don't care about fixing edge cases with peak on max or min freq, they are thrown as non-quality fits anyways
    for(i = peakidx; i % nfreqs != 0 && P_f[i] > factor; i++) {
        ffwhm++; 
    }
    __syncthreads();  

    for(i = peakidx; i % nfreqs != 0 && P_f[i] > factor; i--) {
        ffwhm++; 
    }
    __syncthreads();  // sync threads, they probably diverged during fwhm calculations

    int32_t alfidx = ((peakidx - (peakidx % nfreqs)) % (nfreqs * nalphas)) / nfreqs;
    int32_t freqidx = peakidx % nfreqs;

    // calculate peak freq by looking at neighbors on p_f and calculating normalized momentish calculation
    peakfreq = freqs[freqidx];
    peakalf = alfs[alfidx];
    peakamp = calc_amp(peakalf, env_model, alfidx, freqidx, ce_matrix, se_matrix,  lagmask, s_times, samples, nlags, nfreqs);
    

    // TESTING.. calculate peak SNR and compared to moment SNR
    for (i = 0; i < nlags; i++) {
        int32_t samplebase = threadIdx.x * nlags * 2; 

        float envelope = peakamp * exp(pow(-peakalf * s_times[i], env_model)); 
        float angle = 2 * PI * peakfreq * s_times[i];
        fitted_signal[2*i] = envelope * cos(angle);
        fitted_signal[2*i+1] = envelope * sin(angle);
        
        float rsi = samples[samplebase + 2*i] - fitted_signal[2*i];
        float rsq = samples[samplebase + 2*i+1] - fitted_signal[2*i+1];
        
        rempwr += sqrt(pow(rsi,2) + pow(rsq,2)) * lagmask[threadIdx.x * nlags + i];
        fitpwr += sqrt(pow(fitted_signal[2*i],2) + pow(fitted_signal[2*i+1],2)) * lagmask[threadIdx.x * nlags + i];
    }
    
    snr_peak[threadIdx.x] = fitpwr / rempwr;
    fitpwr = 0;
    rempwr = 0;

    p = calc_peak(peakidx, freqidx, alfidx, nalphas, nlags, nfreqs, P_f, freqs, alfs, ce_matrix, se_matrix, lagmask, s_times, samples, env_model);
    __syncthreads();
    
    peakfreq = p.freq;
    peakalf = p.alf;
    
    amplitudes[threadIdx.x] = peakamp;
    alphafwhm[threadIdx.x] = afwhm;
    freqfwhm[threadIdx.x] = ffwhm;
    
    // on each peak-thread, calculate fitted signal, fitted signal power, and remaining power
    for (i = 0; i < nlags; i++) {
        int32_t samplebase = threadIdx.x * nlags * 2; 

        float envelope = peakamp * exp(-peakalf * s_times[i]); 
        // TODO: add in environment model... exp(-peakalf ** 2?)
        float angle = 2 * PI * peakfreq * s_times[i];
        fitted_signal[2*i] = envelope * cos(angle);
        fitted_signal[2*i+1] = envelope * sin(angle);
        
        samples[samplebase + 2*i] -= fitted_signal[2*i];
        samples[samplebase + 2*i+1] -= fitted_signal[2*i+1];
        
        rempwr += sqrt(pow(samples[samplebase + 2*i],2) + pow(samples[samplebase + 2*i+1],2)) * lagmask[threadIdx.x * nlags + i];
        fitpwr += sqrt(pow(fitted_signal[2*i],2) + pow(fitted_signal[2*i+1],2)) * lagmask[threadIdx.x * nlags + i];
    }
    
    snr[threadIdx.x] = fitpwr / rempwr;
}

// normalize log prob to peak
__device__ peak calc_peak(int32_t peakidx, int32_t freqidx, int32_t alfidx, int32_t nalfs, int32_t nlags, int32_t nfreqs, double *P_f, float *freqs, float *alfs, float *ce_matrix, float *se_matrix, int32_t *lagmask, float *s_times, float *samples, float env_model)
{
    int32_t i;
    int32_t j;
    int32_t reach = (SPOT_WIDTH-1)/2;
    float alf, freq;
    peak p;
    p.freq = 0;
    p.alf = 0;
    p.amp = 0;
    
    // normalize p_f across spot..
    float p_f[SPOT_WIDTH * SPOT_WIDTH];
    float p_f_peak = (float) P_f[peakidx];
    float p_f_sum = 0;

    // de-log and cache spot around peak
    for(i = 0; (i < SPOT_WIDTH) && (alfidx + i - reach < nalfs) && (alfidx + i - reach >= 0); i++ ) {
        for(j = 0; j < SPOT_WIDTH && (freqidx + j - reach < nfreqs) && (freqidx + j - reach >= 0); j++) {
            int32_t idx = peakidx + (j - reach) + (i - reach) * nalfs;
            p_f[i*3 + j] = pow((double) 10.0, P_f[idx] - p_f_peak);
            p_f_sum += p_f[i*3 + j]; 
        }
    }

    // normalize probability and calculate average freq/alf
    for(i = 0; (i < SPOT_WIDTH) && (alfidx + i - reach < nalfs) && (alfidx + i - reach >= 0); i++ ) {
        for(j = 0; j < SPOT_WIDTH && (freqidx + j - reach < nfreqs) && (freqidx + j - reach >= 0); j++) {
            alf = alfs[alfidx + i - reach];
            freq = freqs[freqidx + j - reach];
            p_f[i*3 + j] = p_f[i*3 + j] / p_f_sum;
            p.freq += p_f[i*3 + j] * freq;
            p.alf += p_f[i*3 + j] * alf;
            p.amp += p_f[i*3 + j] * calc_amp(alf, env_model, alfidx + i - reach, freqidx + j - reach, ce_matrix, se_matrix, lagmask, s_times, samples, nlags, nfreqs);
        }
    }
    return p;
}

__device__ float calc_amp(float alf, float env_model, int32_t alfidx, int32_t freqidx, float *ce_matrix, float *se_matrix,  int32_t *lagmask, float *s_times, float *samples, int32_t nlags, int32_t nfreqs)
{
    int32_t i;
    float cs_f = 0;
    float r_f = 0;
    float i_f = 0;

    // calculate cs_f at peak, then calculate amplitude
    for(i = 0; i < nlags; i++) {
        cs_f += pow(exp(pow(-alf * s_times[i], env_model)), 2) * (lagmask[threadIdx.x * nlags + i] != 0);
    }

    // recalculate r_f and i_f at peak 
    for(i = 0; i < nlags; i++) {
        int32_t CS_offset = (alfidx * nfreqs * nlags) + (i * nfreqs) + freqidx;
        int32_t sample_offset = threadIdx.x * nlags * 2 + 2*i;

        r_f += samples[sample_offset + REAL] * ce_matrix[CS_offset] + \
               samples[sample_offset + IMAG] * se_matrix[CS_offset];
        i_f += samples[sample_offset + REAL] * se_matrix[CS_offset] - \
               samples[sample_offset + IMAG] * ce_matrix[CS_offset];
    }
    return (r_f + i_f) / cs_f;
}

""")

# function to calculate P_f on CPU to check GPU calculations
def calculate_bayes(s, t, f, alfs, env_model):
    import numexpr as ne
    ce_matrix, se_matrix, CS_f = make_spacecube(t, f, alfs, env_model)

    N = len(t) * 2.# see equation (10) in [4]
    m = 2
    dbar2 = (sum(np.real(s) ** 2) + sum(np.imag(s) ** 2)) / (N) # (11) in [4] 
    R_f = (np.dot(np.real(s), ce_matrix) + np.dot(np.imag(s), se_matrix)).T
    I_f = (np.dot(np.real(s), se_matrix) - np.dot(np.imag(s), ce_matrix)).T
    
    hbar2 = ne.evaluate('((R_f ** 2) / CS_f + (I_f ** 2) / CS_f)')# (19) in [4] 
    
    P_f = np.log10(N * dbar2 - hbar2)  * ((2 - N) / 2.) - np.log10(CS_f)
    return R_f, I_f, hbar2, P_f

class BayesGPU:
    def __init__(self, lags, freqs, alfs, npulses, env_model):
        self.lags = np.float32(np.array(lags))
        self.freqs = np.float32(np.array(freqs))
        self.alfs = np.float32(np.array(alfs))
       
        self.npulses = npulses
        self.nlags = np.int32(len(self.lags))
        self.nalfs = np.int32(len(self.alfs))
        self.nfreqs = np.int32(len(self.freqs))
      
        self.env_model = np.float32(env_model)

        # do some sanity checks on the input parameters..
        if np.log2(self.nfreqs) != int(np.log2(self.nfreqs)):
            print 'ERROR: number of freqs should be a power of two'

        if self.nfreqs < self.nalfs:
            print 'ERROR: number of alfs should be less than number of freqs'
        
        if self.nfreqs > 1024:
            print 'ERROR: number of frequencies exceeds maximum thread size'
         
        if self.npulses > 1024:
            print 'ERROR: number of pulses exceeds maximum thread size'
         
        if self.npulses <= 1:
            print 'ERROR: number of pulses must be at least 2'

        # create matricies for processing
        ce_matrix, se_matrix, CS_f = make_spacecube(lags, freqs, alfs, env_model)
        ce_matrix_g = np.float32(np.swapaxes(ce_matrix,0,2)).flatten()
        se_matrix_g = np.float32(np.swapaxes(se_matrix,0,2)).flatten()
         
        # create dummy matricies to allocate on GPU
        lagmask = np.int32(np.zeros([self.npulses, self.nlags]))
        samples = np.float32(np.zeros([self.npulses, 2 * self.nlags]))

        self.P_f = np.float64(np.zeros([self.npulses, self.nalfs, self.nfreqs]))
        self.peaks = np.int32(np.zeros(self.npulses))
        self.alf_fwhm = np.int32(np.zeros(self.npulses))
        self.freq_fwhm = np.int32(np.zeros(self.npulses))
        self.amplitudes = np.float64(np.zeros(self.npulses))
        self.dbar2 = np.float64(np.zeros(self.npulses))
        self.snr = np.float32(np.zeros(self.npulses))
        self.snr_peak = np.float32(np.zeros(self.npulses))
        self.n_good_lags = np.int32(np.zeros(self.npulses))
        
        # allocate space on GPU 
        self.samples_gpu = cuda.mem_alloc(samples.nbytes)
        self.lagmask_gpu = cuda.mem_alloc(lagmask.nbytes)
        self.ce_gpu = cuda.mem_alloc(ce_matrix_g.nbytes)
        self.se_gpu = cuda.mem_alloc(se_matrix_g.nbytes)
        self.P_f_gpu = cuda.mem_alloc(self.P_f.nbytes) # 450 mb
        self.peaks_gpu = cuda.mem_alloc(self.peaks.nbytes)
        self.alf_fwhm_gpu = cuda.mem_alloc(self.alf_fwhm.nbytes)
        self.freq_fwhm_gpu = cuda.mem_alloc(self.freq_fwhm.nbytes)
        self.amplitudes_gpu = cuda.mem_alloc(self.amplitudes.nbytes)
        self.n_good_lags_gpu = cuda.mem_alloc(self.n_good_lags.nbytes)
        self.snr_gpu = cuda.mem_alloc(self.snr.nbytes)
        self.snr_peak_gpu= cuda.mem_alloc(self.snr.nbytes)
        self.lag_times_gpu = cuda.mem_alloc(self.lags.nbytes)
        self.freqs_gpu = cuda.mem_alloc(self.freqs.nbytes)
        self.alfs_gpu = cuda.mem_alloc(self.alfs.nbytes)
    
        # compute total GPU memory requirements..

        # copy ce/se/cs matricies over to GPU
        cuda.memcpy_htod(self.ce_gpu, ce_matrix_g)
        cuda.memcpy_htod(self.se_gpu, se_matrix_g)

        # copy over lags, frequencies, and alfs to GPU for SNR calculations
        cuda.memcpy_htod(self.lag_times_gpu, self.lags)
        cuda.memcpy_htod(self.freqs_gpu, self.freqs)
        cuda.memcpy_htod(self.alfs_gpu, self.alfs)

        # get cuda source modules
        self.calc_bayes = mod.get_function('calc_bayes')
        self.find_peaks = mod.get_function('find_peaks')
        self.process_peaks = mod.get_function('process_peaks')

    def run_bayesfit(self, samples, lagmask, copy_samples = True):
        if copy_samples:
            self.lagmask = np.int32(lagmask)
            self.samples = samples
            cuda.memcpy_htod(self.samples_gpu, self.samples)
            cuda.memcpy_htod(self.lagmask_gpu, self.lagmask)
    
        # about 90% of the time is spent on calc_bayes
        self.calc_bayes(self.samples_gpu, self.lagmask_gpu, self.alfs_gpu, self.lag_times_gpu, self.ce_gpu, self.se_gpu, self.P_f_gpu, self.env_model, self.nlags, self.nalfs, self.n_good_lags_gpu, block = (int(self.nfreqs),1,1), grid = (int(self.npulses),1,1))
        self.find_peaks(self.P_f_gpu, self.peaks_gpu, self.nalfs, block = (int(self.nfreqs),1,1), grid = (int(self.npulses),1))
        self.process_peaks(self.samples_gpu, self.ce_gpu, self.se_gpu, self.lag_times_gpu, self.freqs_gpu, self.alfs_gpu, self.P_f_gpu, self.snr_gpu, self.snr_peak_gpu, self.lagmask_gpu, self.n_good_lags_gpu, self.peaks_gpu, self.env_model, self.nfreqs, self.nalfs, self.nlags, self.alf_fwhm_gpu, self.freq_fwhm_gpu, self.amplitudes_gpu, block = (int(self.npulses),1,1))

    
    def process_bayesfit(self, tfreq, noise):
        self.tfreq = tfreq
        self.noise = noise

        cuda.memcpy_dtoh(self.amplitudes, self.amplitudes_gpu)
        cuda.memcpy_dtoh(self.alf_fwhm, self.alf_fwhm_gpu)
        cuda.memcpy_dtoh(self.freq_fwhm, self.freq_fwhm_gpu)
        cuda.memcpy_dtoh(self.peaks, self.peaks_gpu)
        cuda.memcpy_dtoh(self.n_good_lags, self.n_good_lags_gpu)
        cuda.memcpy_dtoh(self.snr, self.snr_gpu)
        cuda.memcpy_dtoh(self.snr_peak, self.snr_peak_gpu)

        dalpha = self.alfs[1] - self.alfs[0]
        dfreqs = self.freqs[1] - self.freqs[0]
        
        N = 2 * self.n_good_lags
        
        w_idx = ((self.peaks - (self.peaks % self.nfreqs)) % (self.nfreqs * self.nalfs)) / self.nfreqs
        v_idx = self.peaks % self.nfreqs

        self.w = (self.alfs[w_idx] * C) / (2. * np.pi * (tfreq * 1e3))
        self.w_std = dalpha * (((C * self.alf_fwhm) / (2. * np.pi * (tfreq * 1e3))) / FWHM_TO_SIGMA)
        self.w_e = self.w_std / np.sqrt(N)
        
        self.v = (self.freqs[v_idx] * C) / (2 * tfreq * 1e3)
        self.v_std = dfreqs * ((((self.freq_fwhm) * C) / (2 * tfreq * 1e3)) / FWHM_TO_SIGMA)
        self.v_e = self.v_std / np.sqrt(N)
        
        self.p = self.amplitudes / noise
        self.p[self.p <= 0] = np.nan
        self.p = 10 * np.log10(self.p)
         
        # raw freq/decay for debugging
        self.vfreq = (self.freqs[v_idx])
        self.walf = (self.alfs[w_idx])
        
        #self.phase_mse = np.zeros(self.npulses) # mse of fitted phase to sample phase for good lags
        #self.envelope_mse = np.zeros(self.npulses) # mse of fitted envelope magnitude to sample good lag magnitudes 
    
        self.phi_sigma = np.zeros(self.npulses)
        self.v_sigma = np.zeros(self.npulses)
        self.slope_sigma = np.zeros(self.npulses)
        # calculate mse for phase, amplitude, and overall signal for fitacf comparison
        for (r, mask) in enumerate(self.lagmask):
            goodmask = (mask == 1)
            lagtimes = self.lags[goodmask]
            samples_i = self.samples[r][0::2][goodmask]
            samples_q = self.samples[r][1::2][goodmask]
            signal = samples_i + 1j * samples_q
            #fitted_envelope = self.amplitudes[r] * np.exp(-self.walf[r] * lagtimes)
            #fitted_angle = 2 * np.pi * self.vfreq[r] * lagtimes
            # we want unwrapped phase, right? otherwise we get artifacts at +/- 2 pi
            #samples_angle = np.angle(samples_i + 1j * samples_q)
            #samples_envelope = np.sqrt((samples_i ** 2) + (samples_q ** 2))
            #self.phase_mse[r] = metrics.mean_squared_error(samples_angle, fitted_angle)
            #self.envelope_mse[r] = metrics.mean_squared_error(samples_envelope, fitted_envelope)
            phi_sigma,slope_sigma,v_sigma = phase_fit_error(signal, lagtimes, self.tfreq * 1000, self.v[r])
            self.phi_sigma[r] = phi_sigma
            self.slope_sigma[r] = slope_sigma
            self.v_sigma[r] = v_sigma
            #self.envelope_mse[r] = metrics.mean_squared_error(samples_envelope, fitted_envelope)


    # pickle a pulse for later analysis
    def pickle_pulse(self, filename='pulse_mcm20140828.p'):
        import pickle
        param = {}
        param['alfs'] = self.alfs
        param['freqs'] = self.freqs
        param['samples'] = self.samples
        param['lagmask'] = self.lagmask
        param['lags'] = self.lags
        param['npulses'] = self.npulses
        param['env_model'] = self.env_model
        param['tfreq'] = self.tfreq
        param['noise'] = self.noise
        pickle.dump(param, open(filename, 'wb'))


# example function to run cuda_bayes against some generated data
# to profile, add @profile atop interesting functions       
# run kernprof -l cuda_bayes.py
# then python -m line_profiler cuda_bayes.py.lprof to view results
def main():
    SYNTHETIC = False 

    f = [4.4]
    amp = [10.]
    alf = [3.5]

    if SYNTHETIC:
        fs = 100.
        ts = 1./fs

        lags = np.arange(0, 24) * ts
        signal = []

        maxpulses = 2
        tfreq = 30 
        noise = .10
        env_model = LAMBDA_FIT 
        lmask = [0, 0, 0, 0, 1, 1, 0, 1, 1, 0, 0, 1, 1, 1, 0, 1, 1, 0, 0, 0, 1, 1, 0]
        lagmask = np.tile(lmask, maxpulses)
        lagmask = np.tile(np.ones(len(lags)), maxpulses)
        
        F = f[0]+noise*np.random.randn()
        U = alf[0]+noise*np.random.randn()
        A = amp[0]+noise*np.random.rand()
       

        for t,lag in enumerate(lags):
            N_I=noise*np.random.randn()
            N_Q=noise*np.random.randn()
            N_A=np.sqrt(N_I**2+N_Q**2)
            N_phase=np.tan(N_Q/N_I)
            N_phase=0#np.tan(N_Q/N_I)
            sig=A * np.exp(1j * 2 * np.pi * F * lag) * np.exp(-U * lag)#N_A*np.exp(1j*N_phase)
            signal.append(sig)
        
        samples = np.tile(np.float32(list(chain.from_iterable(izip(np.real(signal), np.imag(signal))))), maxpulses)

        freqs = np.linspace(-fs/2, fs/2, 64)
        alfs = np.linspace(0,fs/2, 64)

    else: 
        import pickle
        param = pickle.load(open('pulse_mcm2014082804.p', 'rb'))
        lags = param['lags']
        freqs = param['freqs']
        alfs = param['alfs']
        #alfs = alfs[::4]
        #freqs = freqs[::4]
        maxpulses = param['npulses']
        env_model = param['env_model']
        samples = param['samples']
        lagmask = param['lagmask']
        tfreq = param['tfreq']
        noise = param['noise']
    # include all lags..
#    lagmask[:] = 1
#    lagmask[:,0] = 0

    gpu = BayesGPU(lags, freqs, alfs, maxpulses, env_model)
    gpu.run_bayesfit(samples, lagmask)
    gpu.process_bayesfit(tfreq, noise)
    
    rgate = 11
    if SYNTHETIC: 
        rgate = 0
    print 'calculated amplitude: ' + str(gpu.amplitudes[rgate]) 
    print 'calculated freq: ' + str(gpu.vfreq[rgate]) 
    print 'calculated decay: ' + str(gpu.walf[rgate]) 
    
    print 'snr: ' + str(gpu.snr[rgate])
    if not SYNTHETIC: 
        i_samp = samples[rgate][0::2]
        q_samp = samples[rgate][1::2]
        #plt.plot(gpu.lags[np.nonzero(i_samp)], i_samp[np.nonzero(i_samp)])
        #plt.plot(gpu.lags[np.nonzero(q_samp)], q_samp[np.nonzero(q_samp)])
    else:
        pass
        #plt.plot(gpu.lags, np.real(signal))
        #plt.plot(gpu.lags, np.imag(signal))

    #print 'actual decay: ' + str(alf[0])
    #print 'actual freq: ' + str(f[0])
    #print 'actual amplitude: ' + str(amp[0])

    #samples_cpu = np.array(samples[0:int(len(samples)/maxpulses):2]) + 1j * np.array(samples[1:int(len(samples)/maxpulses):2])
    #R_f_cpu, I_f_cpu, P_f_cpu = calculate_bayes(samples_cpu, lags, freqs, alfs, env_model)
    


    ''' 
    if max(P_f_cpu.flatten() - gpu.P_f.flatten()[0:(len(freqs) * len(alfs))]) < 3e-6:
        print 'P_f on GPU and CPU match'
    else:
        print 'P_f calculation error! GPU and CPU matricies do not match'
    '''

    cuda.memcpy_dtoh(gpu.P_f, gpu.P_f_gpu)
    #pdb.set_trace()
    for gate in range(75):
        if gate > 0:
            print gate
            print 'calculated amplitude: ' + str(gpu.amplitudes[gate]) 
            print 'calculated snr: ' + str(gpu.snr[gate]) 
            print 'calculated snr peak: ' + str(gpu.snr_peak[gate]) 
            print 'max P_f: ' + str(max(gpu.P_f[gate].flatten()))
            print 'min P_f: ' + str(min(gpu.P_f[gate].flatten()))
            # so, scale by P_f[0], renormalize again later
            p_f = gpu.P_f[gate]
            p_f -= p_f[0][0] # normalize off first element
            p_f = 10 ** p_f
            p_f /= sum(p_f.flatten())

            fprob = np.sum(p_f, axis=0)
            aprob = np.sum(p_f, axis=1)

            #print 'calculated freq: ' + str(gpu.vfreq[gate]) 
            #print 'mom freq: ' + str(sum(freqs * fprob))

            #print 'calculated decay: ' + str(gpu.walf[gate]) 
            #print 'mom alf: ' + str(sum(alfs * aprob))

            plt.imshow(p_f > max(p_f.flatten()) * .1, interpolation='none')
            plt.imshow(p_f, interpolation='none')
            plt.show()
            fit = gpu.amplitudes[gate] * np.exp(1j * 2 * np.pi * gpu.vfreq[gate] * lags) * np.exp(-gpu.walf[gate] * lags)
            plt.plot(gpu.lags, np.real(fit))
            plt.plot(gpu.lags, np.imag(fit))
            plt.plot(gpu.lags, np.real(samples[gate][0::2]))
            plt.plot(gpu.lags, np.real(samples[gate][1::2]))
            plt.show()
            plt.subplot(111, polar=True)
            plt.plot(np.angle(fit), abs(fit))
            c_samples = np.real(samples[gate][0::2]) + 1j * np.real(samples[gate][1::2]) 
            c_samples = c_samples[c_samples.nonzero()]
            c_mean = np.mean(c_samples)
            plt.plot(np.angle(fit), abs(fit))
            plt.plot(np.angle(c_samples), abs(c_samples))
            plt.plot(np.angle(c_mean), abs(c_mean)) 
            plt.show()
            #pdb.set_trace()
            #plt.plot(lags, np.real(expected))
            #plt.plot(lags, np.imag(expected))
            
            #plt.plot(lags, amp[0] * ce_matrix[0][:,7] + .05)
            #plt.legend(['samp_i', 'samp_q', 'fit_i', 'fit_q', 'expected_i', 'expected_q', 'ce_matrix'])
            #plt.show()
            
       
    '''
    gpu.run_bayesfit(samples, lagmask, copy_samples = False)
    gpu.process_bayesfit(tfreq, noise)
    
    for gate in range(75):
        if gate == 141:
            print gate
            print 'calculated amplitude: ' + str(gpu.amplitudes[gate]) 
            print 'calculated snr: ' + str(gpu.snr[gate]) 
            print 'max P_f: ' + str(max(gpu.P_f[gate].flatten()))
            print 'min P_f: ' + str(min(gpu.P_f[gate].flatten()))
            # so, scale by P_f[0], renormalize again later
            p_f = gpu.P_f[gate]
            p_f -= p_f[0][0] # normalize off first element
            p_f = 10 ** p_f
            p_f /= sum(p_f.flatten())

            fprob = np.sum(p_f, axis=0)
            aprob = np.sum(p_f, axis=1)

            print 'calculated freq: ' + str(gpu.vfreq[gate]) 
            print 'mom freq: ' + str(sum(freqs * fprob))

            print 'calculated decay: ' + str(gpu.walf[gate]) 
            print 'mom alf: ' + str(sum(alfs * aprob))

            plt.imshow(p_f > max(p_f.flatten()) * .1, interpolation='none')
            plt.imshow(p_f, interpolation='none')
            plt.show()
    ''' 
    plt.plot(gpu.snr)
    plt.plot(gpu.snr_peak)
    plt.plot(np.log10(gpu.amplitudes))
    plt.legend(['"moment" fit snr', 'peak fit snr'])
    plt.show()
    pdb.set_trace()

if __name__ == '__main__':
    main()



