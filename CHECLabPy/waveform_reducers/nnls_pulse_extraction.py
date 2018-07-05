from CHECLabPy.core.base_reducer import WaveformReducer
from CHECLabPy.data import get_file
import numpy as np
from scipy import interpolate
from scipy.optimize import nnls
from numba import jit
import scipy

class NNLSPulseExtraction(WaveformReducer):
    """
    Extractor which uses the result of the 
    """

    def __init__(self, n_pixels, n_samples, plot=False,
                 reference_pulse_path='',bases_bin=4,save_waveforms=True, **kwargs):
        super().__init__(n_pixels, n_samples, plot, **kwargs)

        ref = self.load_reference_pulse(reference_pulse_path)
        self.pulse_template, self.norm = ref
        self.save_waveforms = save_waveforms
        self.extracted = None
        self.time_bins=np.arange(128)*1e-9
        self.trange = self.time_bins[-1] - self.time_bins[0]
        self.dt = np.diff(self.time_bins)[0]
        x = np.linspace(0,120e-9,1000)
        y = self.pulse_template(x)
        tt = np.linspace(0,128e-9,1000)

        t_end = x[np.max(np.where(y>0))]
        self.nbasis = int(127+t_end)*bases_bin
        self.basis_t = np.linspace(self.time_bins[0]-t_end, self.time_bins[-2], self.nbasis)
        self.response = np.zeros((len(self.time_bins),self.nbasis))
        for i,t in enumerate(self.basis_t):
            self.response[:,i] = self.pulse_template((self.time_bins-t))
    
    @staticmethod
    def load_reference_pulse(path):
        # file = np.loadtxt(path, delimiter=' ')
        import pickle
        x,y =pickle.load(open('myrefpuls.pkl','rb'))
        k = y[4]/x[4]
        for i in range(4):
            y[i] = k*x[i]

        n = 10
        k = -y[-n]/x[n]
        m = y[-n]-k*x[-n]
        for i in range(1,n+1):
            print(y[-i])
            y[-i] = k*x[-i]+m
            print(y[-i],x[-i])
        pulse_template = scipy.interpolate.InterpolatedUnivariateSpline(x,y,ext=1)
        norm = scipy.integrate.quad(pulse_template,0,36*1e-9)
        return pulse_template,norm
    
    # def get_pulse_height(self, charge):
    #     return charge * self.y_1pe.max()
    
    @jit()
    def _pulse_extraction(self, waveforms):
        px_c = list()
        for i, wf in enumerate(waveforms):
            #Magic number (7) to make it scale similarly to Crosscorrelation
            pcharge = nnls(self.response,wf,tolerance=-14.5,verbose=False) * 7  
            # ptime,pcharge,norm = self._extract_charge(wf)
            #only care about non zero pulses
            m = pcharge>0
            pcharge,ptime =pcharge[m],self.basis_t[m]

            if(len(ptime)>0):
                evt = np.average(ptime,weights=pcharge)
            else:
                evt = 0
            px_c.append((ptime,pcharge,evt))  
        return np.array(px_c)
    
    @jit()
    def _merge_pulses(self, charges,times,binwidth=2.5e-9):
        if(len(charges)==0):
            return times,charges
        binned_charge =[charges[0]]
        binned_time = [times[0]]
        for i in range(1,len(times)):
            if(times[i]-binned_time[-1]<binwidth):
                binned_time[-1] = (binned_time[-1]*binned_charge[-1]+times[i]*charges[i])/(binned_charge[-1]+charges[i])
                binned_charge[-1] +=charges[i]
            else:
                binned_time.append(times[i])
                binned_charge.append(charges[i])

        binned_charge = np.asarray(binned_charge)
        binned_time = np.asarray(binned_time)
        return binned_time, binned_charge


    def _set_t_event(self, waveforms):
        self.extracted = self._pulse_extraction(waveforms)
        self.kwargs['t_event'] = int(np.mean(self.extracted[:,2]*1e9))
        self.kwargs["window_size"] = 20
        self.kwargs["window_shift"] = -25
        super(NNLSPulseExtraction,self)._set_t_event(waveforms)
    
    @jit()
    def _get_charge(self, waveforms):
        charge   = np.zeros(len(self.extracted))
        tcharge  = np.zeros(len(self.extracted))
        tmcharge = np.zeros(len(self.extracted))
        norm     = np.zeros(len(self.extracted))
        npulses  = np.zeros(len(self.extracted))
        self.pulses = dict()
        self.wf     = dict()
        av_ptime    = np.mean(self.extracted[:,2])
        for i,c in enumerate(self.extracted):
            m = c[1]>0
            tm = (np.abs(c[0]-av_ptime)<=5e-9) & m 
            charge[i] = np.sum(c[1][m])
            tcharge[i] = np.sum(c[1][tm])
            if(tcharge[i]>0):
                tmcharge[i] = np.max(c[1][tm])

            npulses[i] = len(c[1][m])
            if(self.save_waveforms and npulses[i]>0):
                self.pulses[i] = np.array(list(zip(c[0][m],c[1][m])))
                self.wf[i] = waveforms[i]

        er = np.ones(len(charge), dtype=bool)
        er[:] =False
        # if(len(self.errata.keys())>0):
            # er[list(self.errata.keys())] =True
        params = dict(
            charge   = charge,
            tcharge  = tcharge,
            tmcharge = tmcharge,
            norm     = norm,
            npulses  = npulses,
            errata   = er
        )
        return params

@jit()
def nnls(A,b,maxit=None,tolerance=0):
    """A mockup of the Lawson/Hanson active set algorithm
   
    See:
    A Comparison of Block Pivoting and Interior-Point Algorithms for Linear Least Squares Problems with Nonnegative Variables
    Author(s): Luis F. Portugal, Joaquim J. Judice, Luis N. Vicente
    Source: Mathematics of Computation, Vol. 63, No. 208 (Oct., 1994), pp. 625-643
    Published by: American Mathematical Society
    Stable URL: http://www.jstor.org/stable/2153286
    """
    
    # let's have the proper shape
    A = np.asarray(A)
    b = np.asarray(b).reshape(b.size)
    # step 0
    F = [] # passive (solved for) set
    G = list(range(A.shape[1])) # active (clamped to zero) set
    x = np.zeros(A.shape[1])
   
    y = -np.dot(A.transpose(),b)
    if(maxit is None):
        maxit = len(b)
   
    iterations = 0
    lstsqs = 0
    
    while True:
            if(iterations>=maxit):
                break
            iterations += 1
            # step 1
            if len(G) == 0:
                    break # the active set is the whole set, we're done
            r_G = y[G].argmin()
            r = G[r_G]
            
            if y[r] >= tolerance:
                    break # x is the optimal solution, we're done
            F.append(r); F.sort()
            G.remove(r)
            feasible = False
            while not feasible:
                    # step 2
                    x_F = np.linalg.lstsq(A[:,F],b,rcond=None)[0]
                    lstsqs += 1
                    if (x_F >= 0).all():
                            x[F] = x_F
                            feasible = True
                    else:
                            # if the new trial solution gained a negative element
                            mask = (x_F <= 0)
                            theta = x[F]/(x[F] - x_F)
                           
                            r_F = theta[mask].argmin()
                            alpha = theta[mask][r_F]
                            r = np.array(F)[mask][r_F]
                            x[F] = x[F] + alpha*(x_F-x[F])
                            F.remove(r)
                            G.append(r); G.sort()
            # step 3
            y[:] = 0
            y[G] = np.dot(A[:,G].transpose(),(np.dot(A[:,F],x[F])-b))
#        
    return x