from datetime import datetime
import logging
import os
import sys

from waitress import serve

if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
    os.chdir(sys._MEIPASS)
    curr_folder = os.path.dirname(sys.executable)
else:
    curr_folder = sys.path[0]

import argparse
import webbrowser

from pcgsepy.config import ACTIVE_LOGGERS, BIN_N
from pcgsepy.evo.fitness import (Fitness, box_filling_fitness,
                                 func_blocks_fitness, mame_fitness,
                                 mami_fitness)
from pcgsepy.evo.genops import expander
from pcgsepy.guis.main_webapp.webapp import app, serve_layout, app_settings
from pcgsepy.mapelites.behaviors import (BehaviorCharacterization, avg_ma,
                                         mame, mami, symmetry)
from pcgsepy.setup_utils import get_default_lsystem, setup_matplotlib
from pcgsepy.mapelites.buffer import Buffer, mean_merge
from pcgsepy.nn.estimators import GaussianEstimator
from pcgsepy.mapelites.map import MAPElites
from pcgsepy.mapelites.emitters import ContextualBanditEmitter, RandomEmitter
from sklearn.gaussian_process.kernels import DotProduct, WhiteKernel

parser = argparse.ArgumentParser()
parser.add_argument("--mapelites_file", help="Location of the MAP-Elites object",
                    type=str, default=None)
parser.add_argument("--dev_mode", help="Launch the webapp in developer mode",
                    action='store_true')
parser.add_argument("--debug", help="Launch the webapp in debug mode",
                    action='store_true')
parser.add_argument("--host", help="Specify host address",
                    type=str, default='127.0.0.1')
parser.add_argument("--port", help="Specify port",
                    type=int, default=8050)

args = parser.parse_args()

logging.getLogger('werkzeug').setLevel(logging.ERROR)

current_datetime = datetime.now().strftime("%Y%m%d%H%M%S")
file_handler = logging.FileHandler(filename=os.path.join(curr_folder, f'log_{current_datetime}.log'),
                                   mode='w+')
file_handler.addFilter(lambda record: record.levelno >= logging.DEBUG)
sysout_handler = logging.StreamHandler(sys.stdout)
sysout_handler.addFilter(lambda record: record.levelno >= (logging.DEBUG if args.debug else logging.INFO))

logging.basicConfig(level=logging.WARNING,
                    format='[%(asctime)s] %(message)s',
                    handlers=[
                        sysout_handler,
                        file_handler
                    ])

for logger_name in ACTIVE_LOGGERS:
    logging.getLogger(logger_name).setLevel(logging.DEBUG)

setup_matplotlib(larger_fonts=False)

used_ll_blocks = [
    #usable blocks
    #Cockpits
    'MyObjectBuilder_Cockpit_OpenCockpitLarge',
    #Energy Generation
    'MyObjectBuilder_Reactor_LargeBlockSmallGenerator',
    #Ship Control
    'MyObjectBuilder_Gyro_LargeBlockGyro',
    #Storage
    'MyObjectBuilder_CargoContainer_LargeBlockSmallContainer',
    #Thrusters
    'MyObjectBuilder_Thrust_LargeBlockSmallThrust',
    #Lights
    'MyObjectBuilder_InteriorLight_SmallLight',
    'MyObjectBuilder_InteriorLight_LargeBlockLight_1corner',
    #Integrity Blocks
    'MyObjectBuilder_CubeBlock_LargeBlockArmorCornerInv',
    'MyObjectBuilder_CubeBlock_LargeBlockArmorCorner',
    'MyObjectBuilder_CubeBlock_LargeBlockArmorSlope',
    'MyObjectBuilder_CubeBlock_LargeBlockArmorBlock',
    'MyObjectBuilder_CubeBlock_Window1x1Slope',
    'MyObjectBuilder_CubeBlock_Window1x1Flat',
]

lsystem = get_default_lsystem(used_ll_blocks=used_ll_blocks)

expander.initialize(rules=lsystem.hl_solver.parser.rules)

feasible_fitnesses = [
    Fitness(name='BoxFilling',
            f=box_filling_fitness,
            bounds=(0, 1)),
    Fitness(name='FuncionalBlocks',
            f=func_blocks_fitness,
            bounds=(0, 1)),
    Fitness(name='MajorMediumProportions',
            f=mame_fitness,
            bounds=(0, 1)),
    Fitness(name='MajorMinimumProportions',
            f=mami_fitness,
            bounds=(0, 1))
]

behavior_descriptors = [
    BehaviorCharacterization(name='Major axis / Medium axis',
                             func=mame,
                             bounds=(0, 6)),
    BehaviorCharacterization(name='Major axis / Smallest axis',
                             func=mami,
                             bounds=(0, 12)),
    BehaviorCharacterization(name='Average Proportions',
                             func=avg_ma,
                             bounds=(0, 10)),
    BehaviorCharacterization(name='Symmetry',
                             func=symmetry,
                             bounds=(0, 1))
]

buffer = Buffer(merge_method=mean_merge)
mapelites = MAPElites(lsystem=lsystem,
                      feasible_fitnesses=feasible_fitnesses,
                      estimator=GaussianEstimator(bound='upper',
                                                  kernel=DotProduct() + WhiteKernel(),
                                                  max_f=sum([f.bounds[1] for f in feasible_fitnesses])),
                      buffer=buffer,
                      behavior_descriptors=behavior_descriptors,
                      n_bins=BIN_N,
                      emitter=ContextualBanditEmitter(estimator='mlp',
                                                      tau=0.5,
                                                      sampling_decay=0.05))
mapelites.allow_aging = False

mapelites.hull_builder.apply_smoothing = False

app_settings.initialize(mapelites=mapelites,
                        dev_mode=args.dev_mode)

app.layout = serve_layout

webapp_url = f'http://{args.host}:{args.port}/'
logging.getLogger().info(f'Serving webapp on http://{args.host}:{args.port}/...')
webbrowser.open_new(webapp_url)

# close the splash screen if launched via application
try:
    import pyi_splash
    if pyi_splash.is_alive():
        pyi_splash.close()
except ModuleNotFoundError as e:
    pass

serve(app.server,
      threads=16,
      host=args.host,
      port=args.port)
