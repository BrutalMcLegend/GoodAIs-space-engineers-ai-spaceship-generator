import configparser
import os

config = configparser.ConfigParser()
curr_dir = os.getcwd()
config.read(os.path.join(curr_dir, 'configs.ini'))

HOST = config['API'].get('host')
PORT = config['API'].getint('port')

# json file with common atoms and action+args
COMMON_ATOMS = config['L-SYSTEM'].get('common_atoms')
# json file with high level atoms and dimensions
HL_ATOMS = config['L-SYSTEM'].get('hl_atoms')
# ranges of parametric l-system
pl_range = config['L-SYSTEM'].get('pl_range').strip().split(',')
PL_LOW, PL_HIGH = int(pl_range[0]), int(pl_range[1])
# required tiles for constraint components_constraint
REQ_TILES = config['L-SYSTEM'].get('req_tiles').split(',')
# L-system variables
# number of iterations (high level)
N_ITERATIONS = config['L-SYSTEM'].getint('n_iterations')
# number of axioms generated at each expansion step
N_APE = config['L-SYSTEM'].getint('n_axioms_generated')

# lower bound for mutation
MUTATION_LOW = config['GENOPS'].getint('mutations_lower_bound')
# upper bound for mutation
MUTATION_HIGH = config['GENOPS'].getint('mutations_upper_bound')
# default number of single axiom's mutations
MUTATION_DEFAULT_N = config['GENOPS'].getint('mutations_n')
