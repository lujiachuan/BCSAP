# -*- coding: utf-8 -*-
import numpy as np
from skopt import gp_minimize
from skopt.space import Real

def fake_objective(x):
    return -10.0 * np.exp(-0.5 * ((x[0]-1.0)**2 + (x[1]+2.0)**2 + (x[2]-0.5)**2))

space = [Real(-5,5), Real(-5,5), Real(-5,5)]
res = gp_minimize(fake_objective, space, n_calls=25, n_initial_points=5, random_state=42, verbose=False)
print("best x:", [f"{v:+.3f}" for v in res.x])
print("best y:", -res.fun)
print("all y:", [f"{-v:.3f}" for v in res.func_vals])
