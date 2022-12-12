import json
import logging
import os
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union
from zipfile import ZipFile, ZIP_DEFLATED

from pcgsepy.guis.voxel import VoxelData

if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
    os.chdir(sys._MEIPASS)
    curr_folder = os.path.dirname(sys.executable)
else:
    curr_folder = sys.path[0]

import dash
import dash_bootstrap_components as dbc
import numpy as np
import plotly.graph_objects as go
from dash import ALL, dcc, html
from dash.dependencies import Input, Output, State
from pcgsepy.common.api_call import block_definitions
from pcgsepy.common.jsonifier import json_dumps
from pcgsepy.common.vecs import Vec
from pcgsepy.guis.main_webapp.modals_msgs import (no_selection_error,
                                                  spaceship_population_help,
                                                  spaceship_preview_help,
                                                  download_help,
                                                  toggle_safe_rules_off_msg,
                                                  toggle_safe_rules_on_msg)
from pcgsepy.guis.utils import AppMode, AppSettings, DashLoggerHandler, Semaphore
from pcgsepy.hullbuilder import HullBuilder, enforce_symmetry
from pcgsepy.lsystem.rules import RuleMaker, StochasticRules
from pcgsepy.lsystem.solution import CandidateSolution
from pcgsepy.mapelites.bin import MAPBin
from pcgsepy.mapelites.emitters import (ContextualBanditEmitter, Emitter,
                                        GreedyEmitter, HumanEmitter,
                                        HumanPrefMatrixEmitter, KNEmitter, KernelEmitter,
                                        PreferenceBanditEmitter, RandomEmitter, SimpleTabularEmitter)
from pcgsepy.mapelites.map import MAPElites, get_elite
from pcgsepy.structure import _is_base_block, _is_transparent_block
from pcgsepy.xml_conversion import convert_structure_to_xml
from tqdm import trange


dashLoggerHandler = DashLoggerHandler()
dashLoggerHandler.addFilter(lambda record: record.levelno >= logging.INFO)
logging.getLogger('webapp').addHandler(dashLoggerHandler)


base_color: Vec = Vec.v3f(0.45, 0.45, 0.45)
block_to_colour: Dict[str, str] = {
    # colours from https://developer.mozilla.org/en-US/docs/Web/CSS/color_value
    'LargeBlockArmorCorner': '#737373',
    'LargeBlockArmorSlope': '#737373',
    'LargeBlockArmorCornerInv': '#737373',
    'LargeBlockArmorBlock': '#737373',
    'LargeBlockGyro': '#2f4f4f',
    'LargeBlockSmallGenerator': '#ffa07a',
    'LargeBlockSmallContainer': '#008b8b',
    'OpenCockpitLarge': '#32cd32',
    'LargeBlockSmallThrust': '#ff8c00',
    'SmallLight': '#fffaf0',
    'Window1x1Slope': '#fffff0',
    'Window1x1Flat': '#fffff0',
    'LargeBlockLight_1corner': '#fffaf0',
    'Unrecognized': '#ff0000',
    'Air': '#000000'
}
hidden_style: Dict[str, str] = {
    'visibility': 'hidden', 'height': '0px', 'display': 'none'}
circle_style: Dict[str, str] = {
    'height': '10px',
    'width': '10px',
    'border-radius': '50%',
    'vertical-align': 'middle',
    'margin': '0 5px 0 0'
}
struct_sizes: Dict[int, str] = {1: 'Small',
                                2: 'Normal',
                                5: 'Large'}


first_launch: bool = True


app_settings = AppSettings()


download_semaphore = Semaphore(locked=True)
process_semaphore = Semaphore()


def resource_path(relative_path: str) -> str:
    """Get the path of the resources. This differs if the app is being launched via script
    or after being compiled to an executable with PyInstaller.

    Args:
        relative_path (str): The relative path to the folder.

    Raises:
        ValueError: Raised if invalid path.

    Returns:
        str: The corrected path to the resources.
    """
    # get absolute path to resource
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


app = dash.Dash(__name__,
                title='AI Spaceship Generator',
                external_stylesheets=[dbc.themes.DARKLY],
                assets_folder=resource_path("assets"),
                update_title=None)


def get_properties_table(cs: Optional[CandidateSolution] = None) -> dbc.Table:
    """Generate the table with the spaceship properties.

    Args:
        cs (Optional[CandidateSolution], optional): The candidate solution. Defaults to None.

    Returns:
        dbc.Table: The table with the properties.
    """
    size = str(cs.size) if cs else '-'
    nblocks = len(cs.content._blocks) if cs else '-'  # cs.n_blocks does not take into account hull
    vol = cs.content.total_volume if cs else '-'
    mass = cs.content.mass if cs else '-'
    struct_size = struct_sizes[cs.content.grid_size] if cs else '-'
    armor_blocks, non_armor_blocks = cs.content.blocks_count if cs else (
        '-', '-')
    cs_unique_blocks = cs.unique_blocks if cs else {}

    table_header = [
        html.Thead(html.Tr([html.Th("Property", style={'text-align': 'center'}),
                            html.Th("Value", style={'text-align': 'center'})]))
    ]
    table_body = [html.Tbody([
        html.Tr([html.Td("Spaceship size"), html.Td(
            f'{size} m', style={'text-align': 'center'})]),
        html.Tr([html.Td("Grid size"), html.Td(
            struct_size, style={'text-align': 'center'})]),
        html.Tr([html.Td("Number of blocks"), html.Td(
            nblocks, style={'text-align': 'center'})]),
        html.Tr([html.Td("Armor blocks"), html.Td(
            armor_blocks, style={'text-align': 'center'})]),
        html.Tr([html.Td("Non-armor blocks"),
                html.Td(non_armor_blocks, style={'text-align': 'center'})]),
        *[html.Tr([html.Td(k), html.Td(v, style={'text-align': 'center'})])
          for k, v in cs_unique_blocks.items()],
        html.Tr([html.Td("Occupied volume"), html.Td(
            f'{vol} m³', style={'text-align': 'center'})]),
        html.Tr([html.Td("Spaceship mass"), html.Td(
            f'{mass} kg', style={'text-align': 'center'})]),
    ])]

    return table_header + table_body


def get_content_legend() -> dbc.Row:
    """Get the legend of the content. Relies on the blocks specified in `block_to_colour`.

    Returns:
        dbc.Row: The legend.
    """
    return dbc.Row([
        dbc.Col(children=[
            html.Span(children=[
                html.P('', style={**circle_style,
                                  **{'background-color': '#%02x%02x%02x' % base_color.scale(256).to_veci().as_tuple() if _is_base_block(block_type) else block_to_colour[block_type]}}),
                dbc.Label(block_type,
                          align='start',
                          #   style={'text-overflow': 'ellipsis', 'text-align': 'left', 'font-size': 'x-small'}
                          )],
                      style={'display': 'inline-flex', 'align-items': 'baseline',
                             'font-size': 'x-small', 'text-align': 'left'}
                      ),
        ],
            style={'overflow': 'hidden', 'text-overflow': 'ellipsis',
                   'display': 'block', 'white-space': 'nowrap'},
            width=3) for block_type in block_to_colour.keys()
    ],
        justify='start')


def serve_layout() -> dbc.Container:
    """Generate the layout of the application.

    Returns:
        dbc.Container: The layout.
    """
    global app_settings

    webapp_info_file = './assets/webapp_help_dev.md' if app_settings.app_mode == AppMode.DEV else './assets/webapp_info.md'
    with open(webapp_info_file, 'r', encoding='utf-8') as f:
        webapp_info_str = f.read()

    algo_info_file = './assets/algo_info.md'
    with open(algo_info_file, 'r', encoding='utf-8') as f:
        algo_info_str = f.read()

    quickstart_usermode_info_file = './assets/quickstart_usermode.md'
    with open(quickstart_usermode_info_file, 'r', encoding='utf-8') as f:
        quickstart_usermode_info_str = f.read()

    webapp_info_modal = dbc.Modal([
        dbc.ModalHeader(dbc.ModalTitle("App Info"),
                        style={'flex-direction': 'column-reverse'},
                        close_button=True),
        dbc.ModalBody(dcc.Markdown(webapp_info_str,
                                   style={'text-align': 'justify'}))
    ],
        id='webapp-info-modal',
        centered=True,
        backdrop='static',
        is_open=False,
        scrollable=True,
        size='lg')

    algo_info_modal = dbc.Modal([
        dbc.ModalHeader(dbc.ModalTitle("AI Info"),
                        style={'flex-direction': 'column-reverse'},
                        close_button=True),
        dbc.ModalBody(dcc.Markdown(algo_info_str,
                                   style={'text-align': 'justify'},
                                   mathjax=True))
    ],
        id='algo-info-modal',
        centered=True,
        backdrop='static',
        is_open=False,
        scrollable=True,
        size='lg')

    quickstart_usermode_modal = dbc.Modal([
        dbc.ModalHeader(dbc.ModalTitle("Tutorial"),
                        style={'flex-direction': 'column-reverse'},
                        close_button=True),
        dbc.ModalBody(dcc.Markdown(quickstart_usermode_info_str,
                                   link_target="_blank",
                                   style={'text-align': 'justify'}))
    ],
        id='quickstart-usermode-modal',
        centered=True,
        backdrop='static',
        is_open=False,
        scrollable=True,
        size='lg')

    no_bins_selected_modal = dbc.Modal(children=[
        dbc.ModalHeader(dbc.ModalTitle("⚠ Warning ⚠"),
                        style={'justify-content': 'center'},
                        close_button=False),
        dbc.ModalBody(no_selection_error),
        dbc.ModalFooter(children=[dbc.Button("Ok",
                                             id="nbs-err-btn",
                                             color="primary",
                                             className="ms-auto",
                                             n_clicks=0)]),
    ],
        id='nbs-err-modal',
        centered=True,
        backdrop=True,
        is_open=False)

    heatmap_help_modal = dbc.Modal([
        dbc.ModalHeader(dbc.ModalTitle("Spaceship Population Help"),
                        style={'justify-content': 'center'},
                        close_button=False),
        dbc.ModalBody(dcc.Markdown(spaceship_population_help,
                                   style={'text-align': 'justify'}))
    ],
        id='hh-modal',
        centered=True,
        backdrop=True,
        is_open=False,
        scrollable=True)

    content_help_modal = dbc.Modal([
        dbc.ModalHeader(dbc.ModalTitle("Selected Spaceship Help"),
                        style={'justify-content': 'center'},
                        close_button=False),
        dbc.ModalBody(dcc.Markdown(spaceship_preview_help,
                                   style={'text-align': 'justify'}))
    ],
        id='ch-modal',
        centered=True,
        backdrop=True,
        is_open=False,
        scrollable=True)

    download_help_modal = dbc.Modal([
        dbc.ModalHeader(dbc.ModalTitle("Spaceship Controls Help"),
                        style={'justify-content': 'center'},
                        close_button=False),
        dbc.ModalBody(dcc.Markdown(download_help,
                                   style={'text-align': 'justify'}))
    ],
        id='dh-modal',
        centered=True,
        backdrop=True,
        is_open=False,
        scrollable=True)

    toggle_unsaferules_modal = dbc.Modal(children=[
        dbc.ModalHeader(dbc.ModalTitle("Turn off safe mode?"),
                        id='sm-modal-title',
                        style={'justify-content': 'center'},
                        close_button=True),
        dbc.ModalBody(children=[
            dcc.Markdown(toggle_safe_rules_off_msg,
                         id='sm-modal-body',
                         style={'text-align': 'justify'}),
            html.Div(id='tsm-body-loading',
                     children=[])
        ]),
        dbc.ModalFooter(children=[dbc.Button("Yes",
                                             id="tsrm-y-btn",
                                             color="primary",
                                             className="ms-auto",
                                             n_clicks=0,
                                             style={'width': '100%'})])
    ],
        id='sm-modal',
        centered=True,
        backdrop='static',
        is_open=False,
        keyboard=False,
        scrollable=True)

    modals = html.Div(children=[
        webapp_info_modal, algo_info_modal, quickstart_usermode_modal, no_bins_selected_modal, heatmap_help_modal, content_help_modal, download_help_modal, toggle_unsaferules_modal
    ])

    header = dbc.Row(children=[
        dbc.Col(html.H1(children=[
            html.Span(children='Space Engineers'),
            html.Br(),
            html.Span(children='🚀AI Spaceship Generator🚀')
        ],
            className='title'),
            width={'size': 6,
                   'offset': 3
                   },
            style={
            'display': 'inline-flex',
            'flex-direction': 'column',
            'align-content': 'center',
            'justify-content': 'center',
            'align-items': 'center'
        }),
        dbc.Col(children=[
            dbc.Row(children=[
                dbc.Col(dbc.Button('Tutorial',
                                   className='button-fullsize',
                                   id='webapp-quickstart-btn',
                                   color='info'),
                        width=4,
                        style=hidden_style if app_settings.app_mode == AppMode.DEV else {}),
                dbc.Col(dbc.Button('App Info',
                                   className='button-fullsize',
                                   id='webapp-info-btn',
                                   color='info'),
                        width=4),
                dbc.Col(dbc.Button('AI Info',
                                   className='button-fullsize',
                                   id='ai-info-btn',
                                   color='info'),
                        width=4)
            ],
                align='center')],
            align='center', width=2)
    ],
        className='header',
        style={'content-justify': 'center'})

    exp_progress = html.Div(
        id='study-progress-div',
        children=[
            dbc.Row(
                dbc.Col(children=[
                    dbc.Label(f'Current Iteration',
                              style={'font-size': 'large'}),
                    dbc.Progress(id="gen-progress",
                                 color='success',
                                 striped=False,
                                 animated=False)
                ],
                    width={'size': 12, 'offset': 0},
                    style={'text-align': 'center'},
                    align='center')
            )
        ])

    mapelites_heatmap = html.Div(children=[

        html.Div(children=[
            html.H4('Spaceship Population',
                    className='section-title'),
            html.Div(children=[
                dbc.Button('🛈',
                           color='info',
                           id='heatmap-help',
                           className='help')],
                     style={'margin': '1vh 1vh 1vh 1vh'})
        ],
            style={
            'display': 'inline-flex',
            'flex-direction': 'row',
            'justify-content': 'center',
            'align-content': 'center',
            'align-items': 'center',
            'text-align': 'center'
        }),

        html.Br(),
        html.Div(className='container',
                 children=[
                     dcc.Graph(id="heatmap-plot",
                            #    figure=go.Figure(data=[]),
                               figure=_build_heatmap(mapelites=app_settings.current_mapelites,
                                                     pop_name='Feasible',
                                                     metric_name='Fitness',
                                                     method_name='Population'),
                               config={
                                   'displayModeBar': False,
                                   'displaylogo': False,
                                   'scrollZoom': True},
                               className='content',
                               style={'z-index': 0, 'overflow': 'auto'}),
                     html.Div(id='heatmap-plot-container',
                              className='overlay',
                              style={'visibility': 'hidden',
                                     'display': 'none',
                                     'pointer-events': 'auto',
                                     'z-index': 1}),
                 ])
    ],
        style={'text-align': 'center'})

    mapelites_controls = html.Div(
        children=[
            html.H4(children='Plot Settings',
                    className='section-title'),
            html.Br(),
            dbc.Label('Choose which population to display.'),
            dbc.DropdownMenu(label='Feasible',
                             children=[
                                 dbc.DropdownMenuItem(
                                     'Feasible', id='population-feasible'),
                                 dbc.DropdownMenuItem(
                                     'Infeasible', id='population-infeasible'),
                             ],
                             id='population-dropdown'),
            html.Br(),
            dbc.Label('Choose which metric to plot.'),
            dbc.DropdownMenu(label='Fitness',
                             children=[
                                 dbc.DropdownMenuItem(
                                     'Fitness', id='metric-fitness'),
                                 dbc.DropdownMenuItem('Age', id='metric-age'),
                                 dbc.DropdownMenuItem(
                                     'Coverage', id='metric-coverage'),
                             ],
                             id='metric-dropdown'),
            html.Br(),
            dbc.Label(
                'Choose whether to compute the metric for the entire bin population or just the elite.'),
            dbc.RadioItems(id='method-radio',
                           options=[
                               {'label': 'Population', 'value': 'Population'},
                               {'label': 'Elite', 'value': 'Elite'}
                           ],
                           value='Population')
        ],
        style=hidden_style if not app_settings.app_mode == AppMode.DEV else {})

    content_plot = html.Div(children=[

        html.Div(children=[
            html.H4('Selected Spaceship',
                    className='section-title'),
            html.Div(children=[
                dbc.Button('🛈',
                           color='info',
                           id='content-help',
                           className='help')],
                     style={'margin': '1vh 1vh 1vh 1vh'})
        ],
            style={
            'display': 'inline-flex',
            'flex-direction': 'row',
            'justify-content': 'center',
            'align-content': 'center',
            'align-items': 'center',
            'text-align': 'center'
        }),

        html.Br(),
        dcc.Graph(id="content-plot",
                #   figure=go.Figure(data=[]),
                  figure=_get_elite_content(mapelites=app_settings.current_mapelites,
                                            bin_idx=None,
                                            pop='Feasible',
                                            camera=None,
                                            show_voxel=False),
                  config={
                      'displayModeBar': False,
                      'displaylogo': False},
                  style={'overflow': 'auto'}),
        html.Div(children=[
            dbc.Switch(
                id="voxel-preview-toggle",
                label="Toggle Voxel Preview",
                value=app_settings.voxelised,
            )
        ],
            style={'display': 'inline-flex', 'justify-content': 'center'}
        ),
        html.Br(),
        dbc.Label('Legend',
                  size='sm'),
        html.Div(children=[
            get_content_legend()
        ],
            id='content-legend-div',
            style={
            'text-align': 'start',
            'border-width': 'thin',
            'border-color': 'whitesmoke',
            'border-style': 'inset'
        })
    ],
        style={'text-align': 'center'})

    color_and_download = html.Div(
        children=[
            dbc.Row(
                dbc.Col([
                    dbc.Label("Spaceship Color",
                              style={'font-size': 'large'}),
                    dbc.Row(children=[
                        dbc.Col(
                            dbc.Input(type="color",
                                      id="color-picker",
                                      value="#737373",
                                      size='lg'),
                            width=9
                        ),
                        dbc.Col(
                            dbc.Button(children='Apply',
                                       id='color-picker-btn',
                                       color='primary',
                                       class_name='button-fullsize'),
                            width=3
                        )
                    ],
                        align='center')],
                    width={'size': 6, 'offset': 3},
                    style={'text-align': 'center'})
            ),
            html.Br(),
            dbc.Row(
                dbc.Col([
                    dbc.Label("Download Blueprint",
                              style={'font-size': 'large'}),
                    html.Br(),
                    dbc.Button('Download',
                               id='download-btn',
                               disabled=False),
                    dcc.Download(id='download-content'),
                    html.Br(),
                    html.Br(),
                    dcc.Loading(id='download-spinner',
                                children='\n\n',
                                fullscreen=False,
                                color='#eeeeee',
                                type='default',
                                style={'justify-content': 'center'}),
                    html.Br()
                ],
                    width={'size': 6, 'offset': 3},
                    style={'text-align': 'center', 'justify-content': 'center'})
            )
        ]
    )

    spaceship_controls = html.Div(
        dbc.Row(
            children=[dbc.Col(children=[
                html.Div(children=[
                    html.H4(children='Spaceship Controls',
                            className='section-title'),
                    html.Div(children=[
                        dbc.Button('🛈',
                                   color='info',
                                   id='download-help',
                                   className='help')],
                     style={'margin': '1vh 1vh 1vh 1vh'})
                ],
                    style={'display': 'inline-flex',
                           'flex-direction': 'row',
                           'justify-content': 'center',
                           'align-items': 'center',
                           'text-align': 'center'}
                ),

                html.Br(),
                color_and_download
            ],
                style={'text-align': 'center'})
            ]))

    content_properties = html.Div(
        children=[
            dbc.Row(children=[
                dbc.Col(children=[
                    dbc.Table(children=get_properties_table(),
                              id='spaceship-properties',
                              bordered=True,
                              color='dark',
                              hover=True,
                              size='sm',
                              responsive=True,
                              striped=True),
                    # TODO move Content String above High-level Rules (on the same row, so pushing it down)
                    html.Div([
                        html.P(children='Content String: '),
                        dbc.Textarea(id='content-string',
                                     value='',
                                     contentEditable=False,
                                     disabled=True,
                                     class_name='content-string-area')
                    ],
                        style={} if app_settings.app_mode == AppMode.DEV else hidden_style)
                ],
                    style={
                    'max-height': '30vh',
                    'overflow': 'auto'
                })],
                align='center')])

    log = html.Div(
        children=[
            html.H4(children='Log',
                    className='section-title'),
            html.Br(),
            dbc.Textarea(id='console-out',
                         value='',
                         wrap=False,
                         contentEditable=False,
                         disabled=True,
                         className='log-area'),
        ])

    properties_panel = html.Div(
        children=[
            dbc.Row(
                dbc.Col([
                    html.H4('Spaceship Properties',
                            className='section-title'),
                    html.Br()
                ])),
            dbc.Row(
                [
                    dbc.Col(content_properties)
                ]
            ),
        ]
    )

    experiment_settings = html.Div(
        children=[
            html.H4(children='Experiment Settings',
                    className='section-title'),
            html.Div(children=[
                 html.Br(),
                    html.Div(children=[
                        html.P(children=f'Selected bin(s): {app_settings.selected_bins}',
                            id='selected-bin')
                    ]),
                    html.Br(),
                ],
                    style={'justify-content': 'center'} if app_settings.app_mode == AppMode.DEV else {**{'justify-content': 'center'}, **hidden_style}),
            dbc.InputGroup(children=[
                dbc.InputGroupText('Feature Descriptors (X, Y):'),
                dbc.DropdownMenu(label=app_settings.current_mapelites.b_descs[0].name,
                                 children=[
                                 dbc.DropdownMenuItem(
                                     b.name, id=f"bc0-{b.name.replace(' / ', '_').replace(' ', '-')}")
                                 for b in app_settings.behavior_descriptors],
                                 id='b0-dropdown'),
                dbc.DropdownMenu(label=app_settings.current_mapelites.b_descs[1].name,
                                 children=[
                                 dbc.DropdownMenuItem(
                                     b.name, id=f"bc1-{b.name.replace(' / ', '_').replace(' ', '-')}")
                                 for b in app_settings.behavior_descriptors],
                                 id='b1-dropdown')
            ],
                className="mb-3",
                style={'justify-content': 'center'} if app_settings.app_mode == AppMode.DEV else {**{'justify-content': 'center'}, **hidden_style}),
            dbc.InputGroup(children=[
                dbc.InputGroupText('Toggle L-system Modules:'),
                dbc.Checklist(id='lsystem-modules',
                              options=[{'label': x.name, 'value': x.name}
                                       for x in app_settings.current_mapelites.lsystem.modules],
                              value=[
                                  x.name for x in app_settings.current_mapelites.lsystem.modules if x.active],
                              inline=True,
                              switch=True)
            ],
                style={'justify-content': 'center'} if app_settings.app_mode == AppMode.DEV else {**{'justify-content': 'center'}, **hidden_style},
                className="mb-3"),
            dbc.InputGroup(children=[
                dbc.InputGroupText('Fitness Weights:'),
                html.Div(children=[
                    html.Div(children=[
                        dbc.Label(children=f.name,
                                  style={'font-size': 'large'}),
                        html.Div(children=[
                            dcc.Slider(min=0,
                                       max=1,
                                       step=0.1,
                                       value=1,
                                       marks=None,
                                       tooltip={"placement": "bottom",
                                                "always_visible": False},
                                       id={'type': 'fitness-sldr',
                                           'index': i})
                        ],
                        )
                    ]) for i, f in enumerate(app_settings.current_mapelites.feasible_fitnesses)
                ])
            ],
                style={'justify-content': 'center'} if app_settings.app_mode == AppMode.DEV else {**{'justify-content': 'center'}, **hidden_style},
                className="mb-3"),
            dbc.InputGroup(children=[
                dbc.InputGroupText('Select Emitter:'),
                dbc.DropdownMenu(label='Random',
                                 children=[
                                     dbc.DropdownMenuItem(
                                         'Human', id='emitter-human'),
                                     dbc.DropdownMenuItem(
                                         'Random', id='emitter-random'),
                                     dbc.DropdownMenuItem(
                                         'Greedy', id='emitter-greedy'),
                                     dbc.DropdownMenuItem(
                                         'Preference Matrix', id='emitter-prefmatrix'),
                                     dbc.DropdownMenuItem(
                                         'Preference Bandit', id='emitter-prefbandit'),
                                     dbc.DropdownMenuItem(
                                         'Contextual Bandit', id='emitter-conbandit'),
                                     dbc.DropdownMenuItem(
                                         'KNN', id='emitter-knn'),
                                     dbc.DropdownMenuItem(
                                         'Linear Kernel', id='emitter-linkernel'),
                                     dbc.DropdownMenuItem(
                                         'RBF Kernel', id='emitter-rbfkernel')
                                 ],
                                 id='emitter-dropdown')
            ],
                className="mb-3",
                style={'justify-content': 'center'} if app_settings.app_mode == AppMode.DEV else {**{'justify-content': 'center'}, **hidden_style}),
            dbc.InputGroup(children=[
                dbc.InputGroupText('Enforce Symmetry:'),
                dbc.DropdownMenu(label='None',
                                 children=[
                                     dbc.DropdownMenuItem(
                                         'None', id='symmetry-none'),
                                     dbc.DropdownMenuItem(
                                         'X-axis', id='symmetry-x'),
                                     dbc.DropdownMenuItem(
                                         'Z-axis', id='symmetry-z'),
                                 ],
                                 id='symmetry-dropdown'),
            ],
                style={'justify-content': 'center'},
                id='symmetry-div',
                className="mb-3"),
            dbc.InputGroup(children=[
                dbc.InputGroupText('Save/Load Population:'),
                dbc.Button(id='popdownload-btn',
                           children='Download Current Population'),
                dcc.Upload(
                    id='popupload-data',
                    children='Upload Population',
                    className='upload',
                    multiple=False
                ),
            ],
                className="mb-3",
                style={'justify-content': 'center'} if app_settings.app_mode == AppMode.DEV else {**{'justify-content': 'center'}, **hidden_style})
        ],
        style={'justify-content': 'center'},
        id='experiment-settings-div')

    experiment_controls = html.Div(
        children=[
            html.H4('Population Controls',
                    className='section-title'),
            dbc.Row(children=[
                html.Br(),
                dbc.Col(children=[
                    dbc.Button(id='step-btn',
                               children='Evolve From Selected Spaceship',
                               className='button-fullsize')
                ],
                    id='step-btn-div',
                    width=6),
                dbc.Col(children=[
                    dbc.Button(id='rand-step-btn',
                               children='Evolve From Random Spaceship',
                               className='button-fullsize')
                ],
                    id='rand-step-btn-div',
                    # style={} if app_settings.app_mode == AppMode.USER else hidden_style,
                    width=6)
            ],
                style={'justify-content': 'center', 'flex-wrap': 'inherit'}),
            dbc.Row(children=[
                dbc.Col(children=[
                    html.Br(),
                    dbc.Button(id='selection-clr-btn',
                               children='Clear Selection',
                               className='button-fullsize')
                ],
                    width=6)],
                style={'justify-content': 'center'} if app_settings.app_mode == AppMode.DEV else {**{'justify-content': 'center'}, **hidden_style}),
            dbc.Row(children=[
                dbc.Col(children=[
                    html.Br(),
                    dbc.Button(id='reset-btn',
                               children='Reinitialize Population',
                               className='button-fullsize')
                ],
                    id='reset-btn-div',
                    style={'justify-content': 'center'},
                    width={'offset': 3, 'size': 6})]),
            dbc.Row(children=[
                dbc.Col(children=[
                    html.Br(),
                    dbc.Switch(id='unsaferules-mode-toggle',
                               label='Toggle Safe Mode',
                               value=app_settings.safe_mode)
                ],
                    width=6)],
                id='unsafemode-div',
                style={'justify-content': 'center', 'text-align': 'center'}),
            dbc.Row(children=[
                dbc.Col(children=[
                    dbc.Label('Evolution Iterations:'),
                    dcc.Slider(min=0,
                            max=10,
                            step=None,
                            value=app_settings.emitter_steps,
                            marks={
                                0: '0',
                                3: '3',
                                5: '5',
                                7: '7',
                                10: '10'
                                },
                            tooltip={"placement": "bottom",
                                        "always_visible": False},
                            id='evo-iter-sldr')
                    ],
                    width=6)
            ],
                    id='emitter-steps-div',
                    style={'justify-content': 'center', 'text-align': 'center'}),
            dbc.Row(children=[
                dbc.Col(children=[
                    html.Br(),
                    dbc.Button(id='selection-btn',
                               children='Toggle Single Bin Selection',
                               className='button-fullsize')
                ],
                    width=6)],
                style={'justify-content': 'center'} if app_settings.app_mode == AppMode.DEV else {**{'justify-content': 'center'}, **hidden_style}),
            dbc.Row(children=[
                dbc.Col(children=[
                    html.Br(),
                    dbc.Button(id='subdivide-btn',
                               children='Subdivide Selected Bin(s)',
                               className='button-fullsize')
                ],
                    width=6)],
                style={'justify-content': 'center'} if app_settings.app_mode == AppMode.DEV else {**{'justify-content': 'center'}, **hidden_style}),
            dbc.Row(children=[
                dbc.Col(children=[
                    html.Br(),
                    dbc.Button(id='download-mapelites-btn',
                               children='Download MAP-Elites',
                               className='button-fullsize'),
                    dcc.Download(id='download-mapelites')
                ],
                    width=6)],
                style={'justify-content': 'center'} if app_settings.app_mode == AppMode.DEV else {**{'justify-content': 'center'}, **hidden_style}),
        ])

    rules = html.Div(
        children=[
            html.H4(children='High-level Rules',
                    className='section-title'),
            html.Br(),
            dbc.Textarea(id='hl-rules',
                         value=str(
                             app_settings.current_mapelites.lsystem.hl_solver.parser.rules),
                         wrap=False,
                         className='rules-area'),
            dbc.Row(
                dbc.Col(dbc.Button(children='Update High-level Rules',
                                   id='update-rules-btn'),
                        width={'size': 4, 'offset': 4}),
                align='center')
        ],
        style={} if app_settings.app_mode == AppMode.DEV else hidden_style)

    progress = html.Div(
        children=[
            dbc.Row(
                dbc.Col(children=[
                    dbc.Label('Evolution Progress',
                              style={'font-size': 'large'}),
                    dbc.Progress(id="step-progress",
                                 color='info',
                                 striped=True,
                                 animated=True)
                ],
                    align='center',
                    style={'text-align': 'center'})
            )
        ],
        id='step-progress-div',
        style={'content-visibility': 'visible' if 0 <= app_settings.step_progress <= 100 else 'hidden',
               'display': 'inline-block' if 0 <= app_settings.step_progress <= 100 else 'none',
               'width': '100%'})

    load_spinner = html.Div(children=[
        dbc.Row(
            dbc.Col(children=[
                dcc.Loading(id='step-spinner',
                            children='',
                            fullscreen=False,
                            color='#eeeeee',
                            type='circle')
            ],
            )
        )

    ])

    intervals = html.Div(
        children=[
            dcc.Interval(id='interval1',
                         interval=1 * 1000,
                         n_intervals=0),
            dcc.Interval(id='interval2',
                         interval=1 * 10,
                         n_intervals=0)
        ]
    )

    return dbc.Container(
        children=[
            modals,
            header,
            dbc.Row(children=[
                dbc.Row(children=[
                    dbc.Col(children=[
                        exp_progress,
                        progress
                    ],
                        width={'offset': 4, 'size': 4}
                    ),
                    dbc.Col(children=[
                        load_spinner
                    ],
                        align='center',
                        width=3)
                ])
            ]),
            html.Br(),
            html.Br(),
            dbc.Row(children=[
                dbc.Col(mapelites_heatmap, width={
                        'size': 3, 'offset': 1}, style={'overflow': 'auto'}),
                dbc.Col(content_plot, width=4, style={'overflow': 'auto'}),
                dbc.Col(properties_panel, width=3)],
                align="start", style={'overflow': 'auto'}),
            html.Br(),
            html.Br(),
            dbc.Row(children=[
                dbc.Col(children=[mapelites_controls,
                                  experiment_controls],
                        width={'size': 3, 'offset': 1}),
                dbc.Col(children=[
                    spaceship_controls,
                    experiment_settings],
                    width=4),
                dbc.Col(children=[log,
                                  rules,
                                  intervals
                                  ],
                        width=3)],
                    align="start"),

            dcc.Download(id='download-population'),
            dcc.Download(id='download-metrics'),

            html.Div(id='hidden-div',
                     children=[],
                     style=hidden_style)
        ],
        fluid=True)


loading_data_component = html.Div(children=[
    dbc.Row(children=[
        dbc.Col(children=[
            dbc.Label("Loading necessary data, this may take a while...     ",
                      size='sm'),
            dbc.Spinner(color="success",
                        type="border",
                        size='sm')
        ],
            align='center',
            style={'justify-content': 'center', 'text-align': 'center'}),
    ])
]
)


# clientside callback to autoscroll log textarea
app.clientside_callback(
    """
    function checkTextareaHeight() {
        var textarea = document.getElementById("console-out");
        if(textarea.selectionStart == textarea.selectionEnd) {
            textarea.scrollTop = textarea.scrollHeight;
        }
        return "";
    }
    """,
    Output("hidden-div", "title"),  # super hacky but Dash leaves me no choice
    Input("interval1", "n_intervals"),
    prevent_initial_call=True
)


@app.callback(
    Output("webapp-info-modal", "is_open"),
    Input("webapp-info-btn", "n_clicks"),
    prevent_initial_call=True
)
def show_webapp_info(n: int) -> bool:
    """Display the "App Info" modal window.

    Args:
        n (int): Number of button clicks.

    Returns:
        bool: `is_open` modal property value.
    """
    return True


@app.callback(
    Output("algo-info-modal", "is_open"),
    Input("ai-info-btn", "n_clicks"),
    prevent_initial_call=True
)
def show_algo_info(n: int) -> bool:
    """Display the "AI Info" modal window.

    Args:
        n (int): Number of button clicks.

    Returns:
        bool: `is_open` modal property value.
    """
    return True


@app.callback(
    Output("hh-modal", "is_open"),
    Input("heatmap-help", "n_clicks"),
    prevent_initial_call=True
)
def show_heatmap_help(n: int) -> bool:
    """Display the "Spaceship Population Help" modal window.

    Args:
        n (int): Number of button clicks.

    Returns:
        bool: `is_open` modal property value.
    """
    return True


@app.callback(
    Output("ch-modal", "is_open"),
    Input("content-help", "n_clicks"),
    prevent_initial_call=True
)
def show_content_help(n: int) -> bool:
    """Display the "Selected Spaceship Help" modal window.

    Args:
        n (int): Number of button clicks.

    Returns:
        bool: `is_open` modal property value.
    """
    return True


@app.callback(
    Output("dh-modal", "is_open"),
    Input("download-help", "n_clicks"),
    prevent_initial_call=True
)
def show_download_help(n: int) -> bool:
    """Display the "Download Help" modal window.

    Args:
        n (int): Number of button clicks.

    Returns:
        bool: `is_open` modal property value.
    """
    return True


@app.callback(Output('console-out', 'value'),
              Input('interval1', 'n_intervals'),
              prevent_initial_call=True)
def update_output(n: int) -> str:
    """Update the `Log` text area.

    Args:
        n (int): Interval time.

    Returns:
        str: The updated content of the text area.
    """
    return ('\n'.join(dashLoggerHandler.queue))


@app.callback(
    [Output("step-progress", "value"),
     Output("step-progress", "label"),
     Output('step-progress-div', 'style')],
    [Input("interval1", "n_intervals")],
    prevent_initial_call=True
)
def update_progress(n: int) -> Tuple[int, str]:
    """Update the `Evolution Progress` progress bar.

    Args:
        n (int): Interval time.

    Returns:
        Tuple[int, str]: The current progress value and string percentage representation.
    """
    return app_settings.step_progress, f"{np.round(app_settings.step_progress, 2)}%", {'content-visibility': 'visible' if app_settings.step_progress >= 0 else 'hidden',
                                                                                       'display': 'inline-block' if app_settings.step_progress >= 0 else 'none',
                                                                                       'width': '100%'}


@app.callback(
    [Output("gen-progress", "value"),
     Output("gen-progress", "label")],
    [Input("interval1", "n_intervals")],
    prevent_initial_call=True
)
def update_gen_progress(n: int) -> Tuple[int, str]:
    """Update the `Current Iteration` progress bar.

    Args:
        n (int): Interval time.

    Returns:
        Tuple[int, str]: The current progress value and string percentage representation.
    """
    return 100, f"{app_settings.gen_counter}"


@app.callback(
    Output("download-mapelites", "data"),
    Input("download-mapelites-btn", "n_clicks"),
    prevent_initial_call=True,
)
def download_mapelites(n_clicks: int) -> Dict[str, str]:
    """Download the current MAP-Elites object.

    Args:
        n_clicks (int): Number of button clicks.

    Returns:
        Dict[str, str]: The `.json` download file.
    """
    t = datetime.now().strftime("%Y%m%d%H%M%S")
    fname = f'{t}_mapelites_{app_settings.current_mapelites.emitter.name}_gen{app_settings.gen_counter:02}'
    logging.getLogger('webapp').info(
        f'The MAP-Elites object will be downloaded shortly.')
    return dict(content=json_dumps(app_settings.current_mapelites), filename=f'{fname}.json')


@app.callback(
    Output("download-content", "data"),
    Output('download-spinner', 'children'),
    State('content-plot', 'figure'),
    State("content-plot", "relayoutData"),
    Input("download-btn", "n_clicks"),
    prevent_initial_call=True,
)
def download_content(curr_content: Dict[str, Any],
                     curr_camera: Dict[str, Any],
                     n: int) -> Tuple[Optional[Dict[str, Any]], str]:
    """Download the selected spaceship as blueprint.

    Args:
        curr_content (Dict[str, Any]): The selected spaceship.
        curr_camera (Dict[str, Any]): The current spaceship preview camera position.
        n (int): Number of button clicks.

    Returns:
        Tuple[Optional[Dict[str, Any]], str]: The `.zip` file containg the spaceship blueprint, and the download spinner text.
    """
    global base_color
    global download_semaphore

    def write_archive(bytes_io):
        with ZipFile(bytes_io, mode="w", compression=ZIP_DEFLATED) as zf:
            # with open('./assets/thumb.png', 'rb') as f:
            #     thumbnail_img = f.read()
            logging.getLogger('webapp').debug(msg=f'[{__name__}.write_archive] Started function, writing zip file...')
            content_fig = go.Figure(data=curr_content['data'],
                                    layout=curr_content['layout'])
            logging.getLogger('webapp').debug(msg=f'[{__name__}.write_archive] Created thumbnail from plot...')
            content_fig.update_layout(scene_camera=curr_camera.get('scene.camera', None))
            logging.getLogger('webapp').debug(msg=f'[{__name__}.write_archive] Updated thumbnail camera...')
            thumbnail_img = content_fig.to_image(format="png")
            logging.getLogger('webapp').debug(msg=f'[{__name__}.write_archive] Converted to image...')
            zf.writestr('thumb.png', thumbnail_img)
            logging.getLogger('webapp').debug(msg=f'[{__name__}.write_archive] Loaded and saved thumbnail.')
            elite = get_elite(mapelites=app_settings.current_mapelites,
                              bin_idx=_switch(
                                  [app_settings.selected_bins[-1]])[0],
                              pop='feasible')
            logging.getLogger('webapp').debug(msg=f'[{__name__}.write_archive] Loaded elite solution {elite=}.')
            if app_settings.symmetry is not None:
                elite.string = enforce_symmetry(string=elite.string,
                                                    axis=app_settings.symmetry)
            tmp = CandidateSolution(string=elite.string)
            tmp.ll_string = elite.ll_string
            tmp.base_color = elite.base_color
            app_settings.current_mapelites.lsystem._set_structure(cs=tmp)
            logging.getLogger('webapp').debug(msg=f'[{__name__}.write_archive] Copied threadsafe elite solution, starting hullbuilding...')
            hullbuilder = HullBuilder(erosion_type=app_settings.current_mapelites.hull_builder.erosion_type,
                                      apply_erosion=True,
                                      apply_smoothing=True)
            logging.getLogger('webapp').debug(msg=f'[{__name__}.write_archive] Added external hull, unlocking semaphore...')
            download_semaphore.unlock()
            download_semaphore._running = 'YES'
            logging.getLogger('webapp').debug(
                f'[{__name__}.write_archive] {download_semaphore.is_locked=}')
            hullbuilder.add_external_hull(tmp.content)
            tmp.content.set_color(tmp.base_color)
            logging.getLogger('webapp').debug(
                f'[{__name__}.write_archive] {tmp.string=}; {tmp.content=}; {tmp.base_color=}')
            zf.writestr('bp.sbc', convert_structure_to_xml(structure=tmp.content,
                        name=f'My Spaceship ({app_settings.rngseed})'))
            logging.getLogger('webapp').debug(msg=f'[{__name__}.write_archive] Created game blueprint, saving spaceship string representation...')
            content_properties = {
                'string': tmp.string,
                'base_color': tmp.base_color.as_dict()
            }
            zf.writestr(f'spaceship_{app_settings.rngseed}_gen{app_settings.gen_counter}', json.dumps(
                content_properties))
            logging.getLogger('webapp').debug(msg=f'[{__name__}.write_archive] Zip file completed and ready to be downloaded.')
            download_semaphore._running = 'NO'

    if app_settings.selected_bins:
        logging.getLogger('webapp').info(
            f'Your selected spaceship will be downloaded shortly.')
        return dcc.send_bytes(write_archive, f'MySpaceship_{app_settings.rngseed}_gen{app_settings.gen_counter}.zip'), '\n\n'
    else:
        return None, '\n\n'


@app.callback(
    Output("evo-iter-sldr", "value"),
    Input("evo-iter-sldr", "value"),
    prevent_initial_call=True
)
def change_emitter_steps(n_steps: int) -> Any:
    logging.getLogger('webapp').info(f'Setting evolution iterations to {n_steps}...')
    app_settings.emitter_steps = n_steps
    return n_steps


@app.callback(Output('step-btn', 'disabled'),
              Output('download-btn', 'disabled'),
              Output('popdownload-btn', 'disabled'),
              Output('rand-step-btn', 'disabled'),
              Output('selection-clr-btn', 'disabled'),
              Output('selection-btn', 'disabled'),
              Output('reset-btn', 'disabled'),
              Output('subdivide-btn', 'disabled'),
              Output('download-mapelites-btn', 'disabled'),
              Output('update-rules-btn', 'disabled'),
              Output('popupload-data', 'disabled'),
              Output('population-dropdown', 'disabled'),
              Output('metric-dropdown', 'disabled'),
              Output('method-radio', 'options'),
              Output('b0-dropdown', 'disabled'),
              Output('b1-dropdown', 'disabled'),
              Output('lsystem-modules', 'options'),
              Output('emitter-dropdown', 'disabled'),
              Output('symmetry-dropdown', 'disabled'),
              Output('color-picker', 'disabled'),
              Output('heatmap-plot-container', 'style'),
              Output({'type': 'fitness-sldr', 'index': ALL}, 'disabled'),
              Output('webapp-quickstart-btn', 'disabled'),
              Output('webapp-info-btn', 'disabled'),
              Output('ai-info-btn', 'disabled'),
              Output('tsrm-y-btn', 'disabled'),
              Output('tsm-body-loading', 'children'),
              Output('color-picker-btn', 'disabled'),
              Output('voxel-preview-toggle', 'disabled'),
              Output('unsaferules-mode-toggle', 'disabled'),
              Output('evo-iter-sldr', 'disabled'),

              State({'type': 'fitness-sldr', 'index': ALL}, 'disabled'),
              State('method-radio', 'options'),
              State('lsystem-modules', 'options'),
              State('tsm-body-loading', 'children'),

              Input('interval2', 'n_intervals'),
              )
def interval_updates(fdis: List[Dict[str, bool]],
                     ms: List[Dict[str, str]],
                     lsysms: List[Dict[str, str]],
                     tsm_loading_data_children: List[Any],
                     ni: int) -> Tuple[Union[bool, Dict[str, Any], List[Any]], ...]:
    """Update the `disable` property of components at every interval.

    Args:
        fdis (List[Dict[str, bool]]): The list of fitness sliders.
        ms (List[Dict[str, str]]): The list of plotting methods.
        lsysms (List[Dict[str, str]]): The list of L-system modules.
        ni (int): The interval value.

    Returns:
        Tuple[Union[bool, Dict[str, Any], List[Any]], ...]: The `disabled` statuses of the components, and other values.
    """
    # non-definitive solution, see: https://github.com/plotly/dash-table/issues/925, https://github.com/plotly/dash/issues/1861
    # long_callback and background callback also do not work (infinite redeployment of webapp)
    global process_semaphore

    running_something = process_semaphore.is_locked

    for o in ms:
        o['disabled'] = running_something
    for o in lsysms:
        o['disabled'] = running_something

    btns = {
        'step-btn.disabled': running_something ,
        'download-btn.disabled': running_something or download_semaphore._running == 'YES',
        'popdownload-btn.disabled': running_something,
        'rand-step-btn.disabled': running_something,
        'selection-clr-btn.disabled': running_something,
        'selection-btn.disabled': running_something,
        'reset-btn.disabled': running_something,
        'subdivide-btn.disabled': running_something,
        'download-mapelites-btn.disabled': running_something,
        'update-rules-btn.disabled': running_something,
        'popupload-data.disabled': running_something,
        'population-dropdown.disabled': running_something,
        'metric-dropdown.disabled': running_something,
        'method-radio.options': ms,
        'b0-dropdown.disabled': running_something,
        'b1-dropdown.disabled': running_something,
        'lsystem-modules.options': lsysms,
        'emitter-dropdown.disabled': running_something,
        'symmetry-dropdown.disabled': running_something,
        'color-picker.disabled': running_something,
        'heatmap-plot-container.style': {'visibility': 'visible' if running_something else 'hidden',
                                         'display': 'grid' if running_something else 'none',
                                         #  'background': '#ffffff11',  # decomment for debugging purposes
                                         'pointer-events': 'auto',
                                         'z-index': 1 if running_something else -1},
        'fitness-sldr.disabled': [running_something] * len(fdis),
        'webapp-quickstart-btn.disabled': running_something,
        'webapp-info-btn.disabled': running_something,
        'ai-info-btn.disabled': running_something,
        'tsrm-y-btn.disabled': running_something,
        'tsm-body-loading.children': [],
        'color-picker-btn.disabled': running_something,
        'voxel-preview-toggle.disabled': running_something,
        'unsaferules-mode-toggle.disabled': running_something,
        'evo-iter-sldr.disabled': running_something,
    }

    if running_something and tsm_loading_data_children == []:
        btns['tsm-body-loading.children'] = loading_data_component
    elif running_something and tsm_loading_data_children != []:
        btns['tsm-body-loading.children'] = dash.no_update

    return tuple(btns.values())


def _switch(ls: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """Switch the elements in a list of tuples.

    Args:
        ls (List[Tuple[int, int]]): The list of tuples.

    Returns:
        List[Tuple[int, int]]: The list of tuples with the elements switched.
    """
    return [(e[1], e[0]) for e in ls]


def _format_bins(mapelites: MAPElites,
                 bins_idx_list: List[Tuple[int, int]],
                 str_prefix: str,
                 do_switch: bool = True,
                 filter_out_empty: bool = True) -> Tuple[List[Tuple[int, int]], str]:
    """Format the list of currently selected bins and create the string representation to display.

    Args:
        mapelites (MAPElites): The MAP-Elites object.
        bins_idx_list (List[Tuple[int, int]]): The list of selected bins.
        str_prefix (str): The prefix of the string representation.
        do_switch (bool, optional): Whether to switch the bins index. Defaults to True.
        filter_out_empty (bool, optional): Whether to remove empty bins from the list. Defaults to True.

    Returns:
        Tuple[List[Tuple[int, int]], str]: The list of selected bins, and the string representation.
    """
    global app_settings
    
    bins_list: List[MAPBin] = [mapelites.bins[j, i]
                               if do_switch else mapelites.bins[i, j] for (i, j) in bins_idx_list]
    sel_bins_str = f'{str_prefix}'
    for b in bins_list:
        i, j = b.bin_idx
        if filter_out_empty:
            if b.non_empty(pop='feasible') or b.non_empty(pop='infeasible'):
                i, j = (j, i) if do_switch else (i, j)
                bc1 = np.sum([mbin.bin_size[0]
                             for mbin in mapelites.bins[:i, j]])
                bc2 = np.sum([mbin.bin_size[1]
                             for mbin in mapelites.bins[i, :j]])
                sel_bins_str += f' {(i, j)} [{bc1}:{bc2}];'
            elif b.bin_idx in bins_idx_list:
                bins_idx_list.remove((i, j))
        else:
            bc1 = np.sum([mbin.bin_size[0] for mbin in mapelites.bins[:i, j]])
            bc2 = np.sum([mbin.bin_size[1] for mbin in mapelites.bins[i, :j]])
            sel_bins_str += f' {(i, j)} [{bc1}:{bc2}];'
    return bins_idx_list, sel_bins_str


def _build_heatmap(mapelites: MAPElites,
                   pop_name: str,
                   metric_name: str,
                   method_name: str) -> go.Figure:
    """Generate the heatmap of the spaceships population.

    Args:
        mapelites (MAPElites): The MAP-Elites object.
        pop_name (str): The name of the population to display.
        metric_name (str): The name of the metric to display.
        method_name (str): The name of the method to use.

    Returns:
        go.Figure: The heatmap figure.
    """
    valid_bins = [x.bin_idx for x in mapelites._valid_bins()]
    metric = app_settings.hm_callback_props['metric'][metric_name]
    use_mean = app_settings.hm_callback_props['method'][method_name]
    population = app_settings.hm_callback_props['pop'][pop_name]
    # build heatmap
    disp_map = np.zeros(shape=mapelites.bins.shape)
    labels = np.zeros(
        shape=(mapelites.bins.shape[1], mapelites.bins.shape[0], 2))
    text = []
    x_labels = np.round(np.cumsum(
        [0] + mapelites.bin_sizes[0][:-1]) + mapelites.b_descs[0].bounds[0], 2)
    y_labels = np.round(np.cumsum(
        [0] + mapelites.bin_sizes[1][:-1]) + mapelites.b_descs[1].bounds[0], 2)
    for i in range(mapelites.bins.shape[0]):
        for j in range(mapelites.bins.shape[1]):
            v = mapelites.bins[i, j].get_metric(metric=metric['name'],
                                                use_mean=use_mean,
                                                population=population)
            disp_map[i, j] = v if v != 0 else None
            s = ''
            if mapelites.bins[i, j].non_empty(pop='feasible'):
                if (i, j) in valid_bins:
                    if (j, i) in app_settings.selected_bins:
                        s = '☑'
                    elif app_settings.gen_counter > 0 and mapelites.bins[i, j].new_elite[population]:
                        s = '▣'
            if j == 0:
                text.append([s])
            else:
                text[-1].append(s)
            labels[j, i, 0] = x_labels[i]
            labels[j, i, 1] = y_labels[j]
    # plot
    hovertemplate = f'{mapelites.b_descs[0].name}: X<br>{mapelites.b_descs[1].name}: Y<br>{metric_name}: Z<extra></extra>'
    hovertemplate = hovertemplate.replace('X', '%{customdata[0]}').replace(
        'Y', '%{customdata[1]}').replace('Z', '%{z}')
    heatmap = go.Figure(
        data=go.Heatmap(
            z=disp_map,
            zmin=0,
            zmax=app_settings.hm_callback_props['metric'][metric_name]['zmax'][population],
            x=np.arange(disp_map.shape[0]),
            y=np.arange(disp_map.shape[1]),
            hoverongaps=False,
            colorscale=app_settings.hm_callback_props['metric'][metric_name]['colorscale'],
            text=text,
            texttemplate='%{text}',
            textfont={"color": 'rgba(238, 238, 238, 1.)'},
            colorbar={"title": {"text": "Fitness",
                                "side": "right"}, 'orientation': 'v'},
            customdata=labels,
        ))
    heatmap.update_xaxes(title=dict(text=mapelites.b_descs[0].name))
    heatmap.update_yaxes(title=dict(text=mapelites.b_descs[1].name))
    heatmap.update_coloraxes(colorbar_title_text=metric_name)
    heatmap.update_layout(autosize=False,
                          dragmode='pan',
                          clickmode='event+select',
                          paper_bgcolor='rgba(0,0,0,0)',
                          plot_bgcolor='rgba(0,0,0,1)',
                          template='plotly_dark',
                          xaxis_showgrid=False,
                          yaxis_showgrid=False,
                          xaxis_zeroline=False,
                          yaxis_zeroline=False,
                          margin=go.layout.Margin(
                              l=0,
                              r=0,
                              b=0,
                              t=0))
    heatmap.update_traces(hovertemplate=hovertemplate)
    heatmap.update_traces(selector=dict(type='heatmap'))
    heatmap.update_layout(
        xaxis={
            'tickvals': np.arange(disp_map.shape[0]),
            'ticktext': x_labels
        },
        yaxis={
            'tickvals': np.arange(disp_map.shape[1]),
            'ticktext': y_labels
        }
    )

    return heatmap


def _get_elite_content(mapelites: MAPElites,
                       bin_idx: Optional[Tuple[int, int]],
                       pop: str,
                       camera: Optional[Dict[str, Any]] = None,
                       show_voxel: bool = False) -> go.Scatter3d:
    """Generate the spaceship preview plot.

    Args:
        mapelites (MAPElites): The MAP-Elites object.
        bin_idx (Optional[Tuple[int, int]]): The index of the selected bin.
        pop (str): The name of the population to pick the elite from.
        camera (Optional[Dict[str, Any]], optional): The current spaceship preview plot camera position. Defaults to None.
        show_voxel (bool, optional): Whether to show the voxel or the scatter preview. Defaults to False.

    Returns:
        go.Scatter3d: The spaceship preview plot.
    """
    if bin_idx is not None:
        # get elite content
        elite = get_elite(mapelites=mapelites,
                          bin_idx=bin_idx,
                          pop=pop)
        if app_settings.symmetry is not None:
            original_string = elite.string
            elite._content = None
            elite.string = enforce_symmetry(string=elite.string,
                                            axis=app_settings.symmetry)
            elite = app_settings.current_mapelites.lsystem._set_structure(cs=app_settings.current_mapelites.lsystem._add_ll_strings(cs=elite),
                                                                          make_graph=False)
            elite.content._air_gridmask = None
            if elite.is_feasible:
                if app_settings.current_mapelites.hull_builder is not None:
                    app_settings.current_mapelites.hull_builder.add_external_hull(structure=elite.content)
            elite.string = original_string
            elite.content.set_color(elite.base_color)
        structure = elite.content
        content = structure.as_grid_array
        arr = np.nonzero(content)
        x, y, z = arr
        fig = go.Figure()

        cs = [content[i, j, k] for i, j, k in zip(x, y, z)]
        unique_blocks = {v: structure._clean_label(
            list(block_definitions.keys())[v - 1]) for v in cs}

        if not show_voxel:
            ss = [structure._clean_label(
                list(block_definitions.keys())[v - 1]) for v in cs]
            custom_colors = []
            for (i, j, k) in zip(x, y, z):
                b = structure._blocks[(
                    i * structure.grid_size, j * structure.grid_size, k * structure.grid_size)]
                if _is_base_block(block_type=structure._clean_label(b.block_type)):
                    custom_colors.append(f'rgb{b.color.as_tuple()}')
                else:
                    custom_colors.append(block_to_colour.get(structure._clean_label(
                        b.block_type), block_to_colour['Unrecognized']))
            # black points for internal air blocks
            air = np.nonzero(structure.air_blocks_gridmask)
            air_x, air_y, air_z = air
            x = np.asarray(x.tolist() + air_x.tolist())
            y = np.asarray(y.tolist() + air_y.tolist())
            z = np.asarray(z.tolist() + air_z.tolist())
            custom_colors.extend([block_to_colour['Air']
                                 for _ in range(len(air_x))])
            ss.extend(['' for _ in range(len(air_x))])
            # create scatter 3d plot
            fig.add_scatter3d(x=x,
                              y=y,
                              z=z,
                              mode='markers',
                              marker=dict(size=4,
                                          line=dict(width=2,
                                                    color='DarkSlateGrey'),
                                          color=custom_colors),
                              hoverinfo='text',
                              text=ss,
                              opacity=1. if not show_voxel else 0.,
                              showlegend=False)

        else:
            transparent_blocks = np.zeros_like(content)
            opaque_blocks = np.zeros_like(content)
            for v, block_type in unique_blocks.items():
                if _is_transparent_block(block_type=block_type):
                    transparent_blocks[content == v] = v
                else:
                    opaque_blocks[content == v] = v

            # add voxel plot
            voxels = VoxelData(opaque_blocks)
            ss = [structure._clean_label(list(block_definitions.keys())[
                                         v - 1]) for v in voxels.intensities]
            indices = {structure._clean_label(
                n): i + 1 for i, n in enumerate(list(block_definitions.keys()))}
            custom_colors = {}
            for k, v in block_to_colour.items():
                if k in indices:
                    if _is_base_block(k):
                        custom_colors[indices[k]
                                      ] = f'rgb{base_color.as_tuple()}'
                    else:
                        custom_colors[indices[k]] = v

            fig.add_mesh3d(x=voxels.vertices[0] - 0.5,
                           y=voxels.vertices[1] - 0.5,
                           z=voxels.vertices[2] - 0.5,
                           i=voxels.triangles[0],
                           j=voxels.triangles[1],
                           k=voxels.triangles[2],
                           facecolor=[custom_colors[ix]
                                      for ix in voxels.intensities],
                           opacity=1.,
                           flatshading=False,
                           showlegend=False,
                           hoverinfo='text',
                           hovertext=ss
                           )

            fig.data[0].update(lighting=dict(ambient=0.55,
                                             diffuse=0.5,
                                             specular=0.75,
                                             roughness=0.25,
                                             fresnel=0.25))

            voxels = VoxelData(transparent_blocks)
            ss = [structure._clean_label(list(block_definitions.keys())[
                                         v - 1]) for v in voxels.intensities]
            indices = {structure._clean_label(
                n): i + 1 for i, n in enumerate(list(block_definitions.keys()))}
            custom_colors = {}
            for k, v in block_to_colour.items():
                if k in indices:
                    if _is_base_block(k):
                        custom_colors[indices[k]
                                      ] = f'rgb{base_color.as_tuple()}'
                    else:
                        custom_colors[indices[k]] = v

            fig.add_mesh3d(x=voxels.vertices[0] - 0.5,
                           y=voxels.vertices[1] - 0.5,
                           z=voxels.vertices[2] - 0.5,
                           i=voxels.triangles[0],
                           j=voxels.triangles[1],
                           k=voxels.triangles[2],
                           facecolor=[custom_colors[ix]
                                      for ix in voxels.intensities],
                           opacity=0.75,
                           flatshading=False,
                           showlegend=False,
                           hoverinfo='text',
                           hovertext=ss
                           )

            fig.data[1].update(lighting=dict(ambient=0.55,
                                             diffuse=0.5,
                                             specular=0.75,
                                             roughness=0.25,
                                             fresnel=0.25))

        # fig.update_traces()
        ux, uy, uz = np.unique(x), np.unique(y), np.unique(z)
        ptg = .2
        show_x = [v for i, v in enumerate(ux) if i % (1 / ptg) == 0]
        show_y = [v for i, v in enumerate(uy) if i % (1 / ptg) == 0]
        show_z = [v for i, v in enumerate(uz) if i % (1 / ptg) == 0]
        fig.update_layout(
            scene=dict(
                xaxis_title='',
                yaxis_title='m',
                zaxis_title='',
                xaxis={
                    'tickvals': show_x,
                    'ticktext': [structure.grid_size * i for i in show_x],
                },
                yaxis={
                    'tickvals': show_y,
                    'ticktext': [structure.grid_size * i for i in show_y],
                },
                zaxis={
                    'tickvals': show_z,
                    'ticktext': [structure.grid_size * i for i in show_z],
                }
            )
        )
    else:
        fig = go.Figure()
        fig.add_mesh3d(x=np.zeros(0, dtype=object),
                       y=np.zeros(0, dtype=object),
                       z=np.zeros(0, dtype=object))

    camera = camera if camera is not None else dict(up=dict(x=0, y=0, z=1),
                                                    center=dict(x=0, y=0, z=0),
                                                    eye=dict(x=2, y=2, z=2))
    fig.update_layout(scene=dict(aspectmode='data'),
                      scene_camera=camera,
                      template='plotly_dark',
                      #   paper_bgcolor='rgba(0,0,0,0)',
                      #   plot_bgcolor='rgba(0,0,0,0)',
                      margin=go.layout.Margin(l=0,
                                              r=0,
                                              b=0,
                                              t=0))

    return fig


def _apply_step(mapelites: MAPElites,
                selected_bins: List[Tuple[int, int]],
                gen_counter: int,
                only_emitter: bool = False) -> bool:
    """Apply a step of FI-2Pop using the human selection and the PLE.

    Args:
        mapelites (MAPElites): The MAP-Elites object.
        selected_bins (List[Tuple[int, int]]): The list of selected bins indices.
        gen_counter (int): The current generation number.
        only_human (bool, optional): Whether to apply only a human step. Defaults to False.
        only_emitter (bool, optional): Whether to apply only a PLE step. Defaults to False.

    Returns:
        bool: Whether the step was applied successfully.
    """
    global app_settings
    global time_elapsed_emitter

    perc_step = 100 / (1 + app_settings.emitter_steps)

    valid = True
    if mapelites.enforce_qnt:
        valid_bins = [x.bin_idx for x in mapelites._valid_bins()]
        for bin_idx in selected_bins:
            valid &= bin_idx in valid_bins
    if valid:
        logging.getLogger('webapp').info(
            msg=f'Started step {gen_counter + 1}...')
        emitter_time = 0
        # reset bins new_elite flags
        mapelites.update_elites(reset=True)
        app_settings.step_progress = 0
        if not only_emitter:
            logging.getLogger('webapp').debug(
                msg=f'[{__name__}._apply_step] human; {selected_bins=}')
            emitter_time += mapelites.interactive_step(bin_idxs=selected_bins,
                                                          gen=gen_counter)
        app_settings.step_progress += perc_step
        logging.getLogger('webapp').info(
            msg=f'Completed step {gen_counter + 1} (created {mapelites.n_new_solutions} solutions); running {app_settings.emitter_steps} additional emitter steps if available...')
        mapelites.n_new_solutions = 0
        if only_emitter:
            tmp_emitter = mapelites.emitter
            mapelites.emitter = RandomEmitter()
        with trange(app_settings.emitter_steps, desc='Emitter steps: ') as iterations:
            for _ in iterations:
                # if not only_human:
                emitter_time += mapelites.emitter_step(gen=gen_counter)
                app_settings.step_progress += perc_step
        if only_emitter:
            mapelites.emitter = tmp_emitter
        logging.getLogger('webapp').info(
            msg=f'Emitter step(s) completed (created {mapelites.n_new_solutions} solutions).')
        mapelites.n_new_solutions = 0
        logging.getLogger('webapp').debug(f'[{__name__}._apply_step] Started updating elites and reassigning content if needed...')
        # TODO: Parallelise if possible
        mapelites.update_elites()
        for (_, _), b in np.ndenumerate(mapelites.bins):
            for pop in ['feasible', 'infeasible']:
                if b.non_empty(pop=pop):
                    e = b.get_elite(population=pop)
                    if e._content is None:
                        mapelites.lsystem._set_structure(cs=e, make_graph=False)
                        mapelites._prepare_cs_content(cs=e)
        app_settings.step_progress = -1
        return True
    else:
        logging.getLogger('webapp').info(
            msg='Step not applied: invalid bin(s) selected.')
        return False


def _update_base_color(color: Vec) -> None:
    """Update the color of base blocks for all spaceships in the current population.

    Args:
        color (Vec): The new color.
    """
    global app_settings
    logging.getLogger('webapp').debug(
        f'[{__name__}._update_base_color] {color=}')
    for (_, _), b in np.ndenumerate(app_settings.current_mapelites.bins):
        for cs in [*b._feasible, *b._infeasible]:
            cs.base_color = color
            if cs._content is not None:
                cs.content.set_color(color)


def __apply_step(**kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Apply a FI-2Pop step and update the application components.

    Returns:
        Dict[str, Any]: The updated application components.
    """
    global population_complexity
    global n_solutions_feas
    global n_solutions_infeas
    global app_settings

    cs_properties = kwargs['cs_properties']
    cs_string = kwargs['cs_string']
    curr_content = kwargs['curr_content']
    curr_heatmap = kwargs['curr_heatmap']
    nbs_err_modal_show = kwargs['nbs_err_modal_show']
    dlbtn_label = kwargs['dlbtn_label']
    curr_camera = kwargs['curr_camera']
    voxel_display = kwargs['curr_voxel_display']

    if app_settings.selected_bins or kwargs['event_trig'] == 'rand-step-btn':
        s = time.perf_counter()
        res = _apply_step(mapelites=app_settings.current_mapelites,
                          selected_bins=_switch(app_settings.selected_bins),
                          gen_counter=app_settings.gen_counter,
                          only_emitter=kwargs['event_trig'] == 'rand-step-btn' and app_settings.app_mode == AppMode.USER)
        if res:
            elapsed = time.perf_counter() - s
            new_complexity = app_settings.current_mapelites.population_complexity(
                pop='feasible')
            app_settings.gen_counter += 1
            if app_settings.selected_bins:
                rem_idxs = []
                for i, b in enumerate(app_settings.selected_bins):
                    # remove preview and properties if last selected bin is now invalid
                    lb = _switch([b])[0]
                    if lb not in [b.bin_idx for b in app_settings.current_mapelites._valid_bins()]:
                        rem_idxs.append(i)
                for i in reversed(rem_idxs):
                    app_settings.selected_bins.pop(i)
                if app_settings.selected_bins == []:
                    curr_content = _get_elite_content(mapelites=app_settings.current_mapelites,
                                                      bin_idx=None,
                                                      pop='')
                    cs_string = ''
                    cs_properties = get_properties_table()
                else:
                    lb = _switch([app_settings.selected_bins[-1]])[0]
                    if app_settings.current_mapelites.bins[app_settings.selected_bins[-1]].new_elite[app_settings.hm_callback_props['pop'][kwargs['pop_name']]]:
                        curr_content = _get_elite_content(mapelites=app_settings.current_mapelites,
                                                          bin_idx=lb,
                                                          pop='feasible' if kwargs['pop_name'] == 'Feasible' else 'infeasible',
                                                          camera=curr_camera.get(
                                                              'scene.camera', None),
                                                          show_voxel=voxel_display)
                        elite = get_elite(mapelites=app_settings.current_mapelites,
                                          bin_idx=lb,
                                          pop='feasible' if kwargs['pop_name'] == 'Feasible' else 'infeasible')
                        cs_string = elite.string
                        cs_properties = get_properties_table(cs=elite)
            # update heatmap
            curr_heatmap = _build_heatmap(mapelites=app_settings.current_mapelites,
                                          pop_name=kwargs['pop_name'],
                                          metric_name=kwargs['metric_name'],
                                          method_name=kwargs['method_name'])
            logging.getLogger('webapp').debug(
                msg=f'[{__name__}.__apply_step] {elapsed=}; {new_complexity=}; {app_settings.gen_counter=}; {app_settings.selected_bins=}')

    else:
        logging.getLogger('webapp').error(
            msg=f'Step not applied: no bin(s) selected.')
        nbs_err_modal_show = True

    return {
        'content-plot.figure': curr_content,
        'content-string.value': cs_string,
        'heatmap-plot.figure': curr_heatmap,
        'nbs-err-modal.is_open': nbs_err_modal_show,
        'spaceship-properties.children': cs_properties,
        'download-btn.children': dlbtn_label
    }


def __reset(**kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Reset the current MAP-Elites object and update the application components.

    Returns:
        Dict[str, Any]: The updated application components.
    """
    global app_settings
    global n_spaceships_inspected
    global time_elapsed_emitter
    global population_complexity
    global n_solutions_feas
    global n_solutions_infeas

    logging.getLogger('webapp').info(
        msg='Started resetting all bins (this may take a while)...')
    app_settings.current_mapelites.reset()
    logging.getLogger('webapp').info(msg='Reset completed.')
    app_settings.gen_counter = 0
    app_settings.selected_bins = []
    _update_base_color(color=base_color)

    return {
        'heatmap-plot.figure': _build_heatmap(mapelites=app_settings.current_mapelites,
                                              pop_name=kwargs['pop_name'],
                                              metric_name=kwargs['metric_name'],
                                              method_name=kwargs['method_name']),
        'content-plot.figure': _get_elite_content(mapelites=app_settings.current_mapelites,
                                                  bin_idx=None,
                                                  pop=None),
        'spaceship-properties.children': get_properties_table(cs=None)
    }


def __bc_change(**kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Change the MAP-Elites' behavioral characterisatons object and update the application components.

    Returns:
        Dict[str, Any]: The updated application components.
    """
    global app_settings

    event_trig = kwargs['event_trig']
    b0 = kwargs['b0']
    b1 = kwargs['b1']
    curr_heatmap = kwargs['curr_heatmap']

    if event_trig.startswith('bc0') or event_trig.startswith('bc1'):
        if event_trig.startswith('bc0'):
            b0 = event_trig.replace(
                'bc0-', '').replace('_', ' / ').replace('-', ' ')
        else:
            b1 = event_trig.replace(
                'bc1-', '').replace('_', ' / ').replace('-', ' ')
        logging.getLogger('webapp').info(
            msg=f'Updating feature descriptors to ({b0}, {b1})...')
        b0 = app_settings.behavior_descriptors[[
            b.name for b in app_settings.behavior_descriptors].index(b0)]
        b1 = app_settings.behavior_descriptors[[
            b.name for b in app_settings.behavior_descriptors].index(b1)]
        app_settings.current_mapelites.update_behavior_descriptors((b0, b1))
        curr_heatmap = _build_heatmap(mapelites=app_settings.current_mapelites,
                                      pop_name=kwargs['pop_name'],
                                      metric_name=kwargs['metric_name'],
                                      method_name=kwargs['method_name'])
        logging.getLogger('webapp').info(
            msg='Feature descriptors update completed.')
    else:
        logging.getLogger('webapp').error(
            msg=f'Could not change BC: passed unrecognized value ({event_trig}).')

    return {
        'heatmap-plot.figure': curr_heatmap,
    }


def __subdivide(**kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Subdivide the selected bins in the MAP-Elites object and update the application components.

    Returns:
        Dict[str, Any]: The updated application components.
    """
    global app_settings

    curr_heatmap = kwargs['curr_heatmap']

    bin_idxs = [(x[1], x[0]) for x in app_settings.selected_bins]
    for bin_idx in bin_idxs:
        logging.getLogger('webapp').debug(
            msg=f'[{__name__}.__subdivide] Subdividing {bin_idx=}')
        app_settings.current_mapelites.subdivide_range(bin_idx=bin_idx)
    curr_heatmap = _build_heatmap(mapelites=app_settings.current_mapelites,
                                  pop_name=kwargs['pop_name'],
                                  metric_name=kwargs['metric_name'],
                                  method_name=kwargs['method_name'])
    logging.getLogger('webapp').info(
        msg=f'Subdivided bin(s): {app_settings.selected_bins}.')
    app_settings.selected_bins = []

    return {
        'heatmap-plot.figure': curr_heatmap,
    }


def __lsystem_modules(**kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Toggle the L-system modules mutability and update the application components.

    Returns:
        Dict[str, Any]: The updated application components.
    """
    global app_settings

    modules = kwargs['modules']

    all_modules = [x for x in app_settings.current_mapelites.lsystem.modules]
    names = [x.name for x in all_modules]
    for i, module in enumerate(names):
        if module in modules and not all_modules[i].active:
            # activate module
            app_settings.current_mapelites.toggle_module_mutability(
                module=module)
            logging.getLogger('webapp').debug(
                msg=f'[{__name__}.__subdivide] Enabled {module}')
            break
        elif module not in modules and all_modules[i].active:
            # deactivate module
            app_settings.current_mapelites.toggle_module_mutability(
                module=module)
            logging.getLogger('webapp').debug(
                msg=f'[{__name__}.__subdivide] Disabled {module}')
            break
    logging.getLogger('webapp').info(msg=f'L-system modules updated')

    return {}


def __update_rules(**kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Update the L-system expansion rules and update the application components.

    Returns:
        Dict[str, Any]: The updated application components.
    """
    global app_settings

    rules = kwargs['rules']

    new_rules = StochasticRules()
    for rule in rules.split('\n'):
        lhs, p, rhs = rule.strip().split(' ')
        new_rules.add_rule(lhs=lhs,
                           rhs=rhs,
                           p=float(p))
    try:
        new_rules.validate()
        app_settings.current_mapelites.lsystem.hl_solver.parser.rules = new_rules
        logging.getLogger('webapp').info(msg=f'L-system rules updated.')
    except AssertionError as e:
        logging.getLogger('webapp').info(
            msg=f'Failed updating L-system rules ({e}).')

    return {
        'hl-rules.value': str(app_settings.current_mapelites.lsystem.hl_solver.parser.rules)
    }


def __fitness_weights(**kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Update the MAP-Elites fitness weights and update the application components.

    Returns:
        Dict[str, Any]: The updated application components.
    """
    global app_settings

    curr_heatmap = kwargs['curr_heatmap']
    weights = kwargs['weights']

    app_settings.current_mapelites.update_fitness_weights(weights=weights)
    logging.getLogger('webapp').info(msg='Updated fitness functions weights.')
    app_settings.hm_callback_props['metric']['Fitness']['zmax']['feasible'] = sum(
        [x.weight * x.bounds[1] for x in app_settings.current_mapelites.feasible_fitnesses]) + app_settings.current_mapelites.nsc

    curr_heatmap = _build_heatmap(mapelites=app_settings.current_mapelites,
                                  pop_name=kwargs['pop_name'],
                                  metric_name=kwargs['metric_name'],
                                  method_name=kwargs['method_name'])

    return {
        'heatmap-plot.figure': curr_heatmap,
    }


def __update_heatmap(**kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Update the MAP-Elites grid heatmap and update the application components.

    Returns:
        Dict[str, Any]: The updated application components.
    """
    global app_settings

    event_trig = kwargs['event_trig']
    pop_name = kwargs['pop_name']
    metric_name = kwargs['metric_name']
    method_name = kwargs['method_name']

    if event_trig == 'population-feasible':
        pop_name = 'Feasible'
    elif event_trig == 'population-infeasible':
        pop_name = 'Infeasible'
    elif event_trig == 'metric-fitness':
        metric_name = 'Fitness'
    elif event_trig == 'metric-age':
        metric_name = 'Age'
    elif event_trig == 'metric-coverage':
        metric_name = 'Coverage'
    logging.getLogger('webapp').debug(
        msg=f'[{__name__}.__update_heatmap] {pop_name=}; {metric_name=}; {method_name=}')

    curr_heatmap = _build_heatmap(mapelites=app_settings.current_mapelites,
                                  pop_name=pop_name,
                                  metric_name=metric_name,
                                  method_name=method_name)
    return {
        'heatmap-plot.figure': curr_heatmap,
    }


def __apply_symmetry(**kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Apply the symmetry to the spaceships and update the application components.

    Returns:
        Dict[str, Any]: The updated application components.
    """
    global app_settings

    event_trig = kwargs['event_trig']

    logging.getLogger('webapp').info(
        msg=f'Updating all solutions to enforce symmetry...')
    if event_trig == 'symmetry-none':
        symm_axis = 'None'
    elif event_trig == 'symmetry-x':
        symm_axis = 'X-axis'
    elif event_trig == 'symmetry-z':
        symm_axis = 'Z-axis'
    logging.getLogger('webapp').debug(
        msg=f'[{__name__}.__apply_symmetry] {symm_axis=}')

    app_settings.symmetry = symm_axis[0].lower() if symm_axis != "None" else None
    app_settings.selected_bins = []
    curr_heatmap = _build_heatmap(mapelites=app_settings.current_mapelites,
                                  pop_name=kwargs['pop_name'],
                                  metric_name=kwargs['metric_name'],
                                  method_name=kwargs['method_name'])
    curr_content = _get_elite_content(mapelites=app_settings.current_mapelites,
                                      bin_idx=None,
                                      pop=None)
    logging.getLogger('webapp').info(msg=f'Symmetry enforcement completed.')

    return {
        'content-plot.figure': curr_content,
        'heatmap-plot.figure': curr_heatmap,
        'content-string.value': '',
        'symmetry-dropdown.label': symm_axis,
        'spaceship-properties.children': get_properties_table()
    }


def __update_content(**kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Update the spaceship preview plot with a new selection and update the application components.

    Returns:
        Dict[str, Any]: The updated application components.
    """
    global app_settings

    curr_heatmap = kwargs['curr_heatmap']
    curr_content = kwargs['curr_content']
    cs_string = kwargs['cs_string']
    cs_properties = kwargs['cs_properties']
    voxel_display = kwargs['curr_voxel_display']

    i, j = kwargs['clickData']['points'][0]['x'], kwargs['clickData']['points'][0]['y']
    if app_settings.current_mapelites.bins[j, i].non_empty(pop='feasible' if kwargs['pop_name'] == 'Feasible' else 'infeasible'):
        if (j, i) in [b.bin_idx for b in app_settings.current_mapelites._valid_bins()]:
            curr_content = _get_elite_content(mapelites=app_settings.current_mapelites,
                                              bin_idx=(j, i),
                                              pop='feasible' if kwargs['pop_name'] == 'Feasible' else 'infeasible',
                                              camera=None,
                                              show_voxel=voxel_display)
            if not app_settings.current_mapelites.enforce_qnt and app_settings.selected_bins != []:
                if (i, j) not in app_settings.selected_bins:
                    app_settings.selected_bins.append((i, j))
                else:
                    app_settings.selected_bins.remove((i, j))
            else:
                app_settings.selected_bins = [(i, j)]
            cs_string = ''
            cs_properties = get_properties_table()
            if len(app_settings.selected_bins) > 0:
                elite = get_elite(mapelites=app_settings.current_mapelites,
                                  bin_idx=_switch(
                                      [app_settings.selected_bins[-1]])[0],
                                  pop='feasible' if kwargs['pop_name'] == 'Feasible' else 'infeasible')
                cs_string = elite.string
                cs_properties = get_properties_table(cs=elite)
                curr_heatmap = _build_heatmap(mapelites=app_settings.current_mapelites,
                                              pop_name=kwargs['pop_name'],
                                              metric_name=kwargs['metric_name'],
                                              method_name=kwargs['method_name'])
    else:
        logging.getLogger('webapp').error(
            msg=f'[{__name__}.__update_content] Empty bin selected: {(i, j)=}')

    return {
        'heatmap-plot.figure': curr_heatmap,
        'content-plot.figure': curr_content,
        'content-string.value': cs_string,
        'spaceship-properties.children': cs_properties,
    }


def __toggle_voxelization(**kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Toggle the voxelization in the spaceship preview plot and update the application components.

    Returns:
        Dict[str, Any]: The updated application components.
    """
    global app_settings

    curr_content = kwargs['curr_content']
    curr_camera = kwargs['curr_camera']
    voxel_display = kwargs['curr_voxel_display']
    app_settings.voxelised = voxel_display

    if app_settings.selected_bins:
        lb = _switch([app_settings.selected_bins[-1]])[0]
        curr_content = _get_elite_content(mapelites=app_settings.current_mapelites,
                                        bin_idx=lb,
                                        pop='feasible' if kwargs['pop_name'] == 'Feasible' else 'infeasible',
                                        camera=curr_camera.get(
                                            'scene.camera', None),
                                        show_voxel=voxel_display)

    return {
        'content-plot.figure': curr_content,
    }


def __selection(**kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Toggle the MAP-Elites bins selection limit and update the application components.

    Returns:
        Dict[str, Any]: The updated application components.
    """
    global app_settings

    app_settings.current_mapelites.enforce_qnt = not app_settings.current_mapelites.enforce_qnt
    logging.getLogger('webapp').info(
        msg=f'MAP-Elites single bin selection set to {app_settings.current_mapelites.enforce_qnt}.')
    if app_settings.current_mapelites.enforce_qnt and app_settings.selected_bins:
        app_settings.selected_bins = [app_settings.selected_bins[-1]]

    return {}


def __clear_selection(**kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Clear the bins selection and update the application components.

    Returns:
        Dict[str, Any]: The updated application components.
    """
    global app_settings

    logging.getLogger('webapp').info(msg='Cleared bins selection.')
    app_settings.selected_bins = []

    return {
        'content-plot.figure':  _get_elite_content(mapelites=app_settings.current_mapelites,
                                                   bin_idx=None,
                                                   pop=None),
        'content-string.value': '',
        'spaceship-properties.children': get_properties_table(),
    }


def __emitter(**kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Set the new emitter to the MAP-Elites object and update the application components.

    Returns:
        Dict[str, Any]: The updated application components.
    """
    global app_settings

    emitter_name = {
        'emitter-human': 'Human',
        'emitter-random': 'Random',
        'emitter-greedy': 'Greedy',
        'emitter-prefmatrix': 'Preference Matrix',
        'emitter-prefbandit': 'Preference Bandit',
        'emitter-conbandit': 'Contextual Bandit',
        'emitter-knn': 'KNN',
        'emitter-linkernel': 'Linear Kernel',
        'emitter-rbfkernel': 'RBF Kernel'
    }[kwargs['event_trig']]

    if emitter_name == 'Random':
        app_settings.current_mapelites.emitter = RandomEmitter()
        logging.getLogger('webapp').info(msg=f'Emitter set to {emitter_name}')
    if emitter_name == 'Greedy':
        app_settings.current_mapelites.emitter = GreedyEmitter()
        logging.getLogger('webapp').info(msg=f'Emitter set to {emitter_name}')
    elif emitter_name == 'Preference-matrix':
        app_settings.current_mapelites.emitter = HumanPrefMatrixEmitter()
        app_settings.current_mapelites.emitter._build_pref_matrix(
            bins=app_settings.current_mapelites.bins)
        logging.getLogger('webapp').info(msg=f'Emitter set to {emitter_name}')
    elif emitter_name == 'Contextual Bandit':
        app_settings.current_mapelites.emitter = ContextualBanditEmitter()
        logging.getLogger('webapp').info(msg=f'Emitter set to {emitter_name}')
    elif emitter_name == 'Preference Bandit':
        app_settings.current_mapelites.emitter = PreferenceBanditEmitter()
        logging.getLogger('webapp').info(msg=f'Emitter set to {emitter_name}')
    elif emitter_name == 'KNN':
        app_settings.current_mapelites.emitter = KNEmitter()
        logging.getLogger('webapp').info(msg=f'Emitter set to {emitter_name}')
    elif emitter_name == 'Linear Kernel':
        app_settings.current_mapelites.emitter = KernelEmitter()
        logging.getLogger('webapp').info(msg=f'Emitter set to {emitter_name}')
    elif emitter_name == 'Human':
        app_settings.current_mapelites.emitter = HumanEmitter()
        logging.getLogger('webapp').info(msg=f'Emitter set to {emitter_name}')
    else:
        logging.getLogger('webapp').error(
            msg=f'[{__name__}.__emitter] Unrecognized {emitter_name=}')

    return {
        'emitter-dropdown.label': emitter_name
    }


def __population_download(**kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Download the current spaceships population and update the application components.

    Returns:
        Dict[str, Any]: The updated application components.
    """
    global app_settings

    content_dl = dict(content=json.dumps([b.to_json() for b in app_settings.current_mapelites.bins.flatten().tolist()]),
                      filename=f'population_{app_settings.rngseed}_exp{app_settings.gen_counter}_{app_settings.current_mapelites.emitter.name}.json')
    logging.getLogger('webapp').info(
        f'The population will be downloaded shortly.')
    return {
        'download-population.data': content_dl
    }


def __population_upload(**kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Upload the spaceships population from file and update the application components.

    Returns:
        Dict[str, Any]: The updated application components.
    """
    global app_settings

    upload_filename = kwargs['upload_filename']

    logging.getLogger('webapp').info(msg=f'Setting population from file...')
    app_settings.current_mapelites.load_population(filename=upload_filename)
    logging.getLogger('webapp').info(
        msg=f'Set population from file successfully.')
    app_settings.gen_counter = 0
    app_settings.selected_bins = []

    return {
        'heatmap-plot.figure': _build_heatmap(mapelites=app_settings.current_mapelites,
                                              pop_name=kwargs['pop_name'],
                                              metric_name=kwargs['metric_name'],
                                              method_name=kwargs['method_name']),
        'content-plot.figure': _get_elite_content(mapelites=app_settings.current_mapelites,
                                                  bin_idx=None,
                                                  pop=None),
        'spaceship-properties.children': get_properties_table(cs=None)
    }


def __close_error(**kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Close the error modal and update the application components.

    Returns:
        Dict[str, Any]: The updated application components.
    """
    return {'nbs-err-modal.is_open': False}


def __color(**kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Update the spaceships base blocks color and update the application components.

    Returns:
        Dict[str, Any]: The updated application components.
    """
    global app_settings
    global base_color

    color = kwargs['color']
    curr_content = kwargs['curr_content']
    curr_camera = kwargs['curr_camera']
    voxel_display = kwargs['curr_voxel_display']

    r, g, b = tuple(int(color.lstrip('#')[i:i+2], 16) for i in (0, 2, 4))
    new_color = Vec.v3f(r, g, b).scale(1 / 256)
    base_color = new_color
    logging.getLogger('webapp').debug(
        msg=f'[{__name__}.__color] {base_color=}')
    _update_base_color(color=base_color)
    if app_settings.selected_bins:
        curr_content = _get_elite_content(mapelites=app_settings.current_mapelites,
                                          bin_idx=_switch(
                                              [app_settings.selected_bins[-1]])[0],
                                          pop='feasible' if kwargs['pop_name'] == 'Feasible' else 'infeasible',
                                          camera=curr_camera.get(
                                              'scene.camera', None),
                                          show_voxel=voxel_display)
    return {
        'content-plot.figure': curr_content,
        'content-legend-div.children': get_content_legend()
    }


def __show_quickstart_modal(**kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Show the quickstart modal and update the application components.
    Returns:
        Dict[str, Any]: The updated application components.
    """
    return {
        'quickstart-usermode-modal.is_open': True
    }


def __default(**kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Fallback callback execution method.

    Returns:
        Dict[str, Any]: The updated application components.
    """
    global app_settings

    return {
        'heatmap-plot.figure': _build_heatmap(mapelites=app_settings.current_mapelites,
                                              pop_name=kwargs['pop_name'],
                                              metric_name=kwargs['metric_name'],
                                              method_name=kwargs['method_name']),
        'content-plot.figure': _get_elite_content(mapelites=app_settings.current_mapelites,
                                                  bin_idx=_switch([app_settings.selected_bins[-1]])[0] if app_settings.selected_bins else None,
                                                  pop='feasible' if kwargs['pop_name'] == 'Feasible' else 'infeasible')
    }


def __show_toggle_unsafe_mode_modal(**kwargs) -> Dict[str, Any]:
    curr_unsafemode = kwargs['curr_unsafemode']
    
    return {
        'sm-modal.is_open': True,
        'unsaferules-mode-toggle.value': not curr_unsafemode
    }


def __toggle_unsafe_mode(**kwargs) -> Dict[str, Any]:
    curr_unsafemode = kwargs['curr_unsafemode']
    logging.getLogger('webapp').info(msg=f'Toggling safe mode to {not curr_unsafemode}')
    logging.getLogger('webapp').debug(msg=f'[{__name__}.__toggle_unsafe_mode] Current safe mode is {curr_unsafemode}')
    ruleset = 'hlrules' if curr_unsafemode else 'hlrules_sm'
    logging.getLogger('webapp').debug(msg=f'[{__name__}.__toggle_unsafe_mode] New HL ruleset is {ruleset}')
    try:
        new_rules = RuleMaker(ruleset=ruleset).get_rules()
        app_settings.current_mapelites.lsystem.hl_solver.parser.rules = new_rules
        logging.getLogger('webapp').info(msg=f'L-system rules updated.')
    except AssertionError as e:
        logging.getLogger('webapp').warn(msg=f'Failed updating L-system rules ({e}).')
    logging.getLogger('webapp').info(msg='Started resetting all bins (this may take a while)...')
    app_settings.current_mapelites.hull_builder.apply_smoothing = False
    app_settings.current_mapelites.reset()
    logging.getLogger('webapp').info(msg='Reset completed.')
    app_settings.gen_counter = 0
    app_settings.selected_bins = []
    app_settings.safe_mode = not curr_unsafemode
    
    return {
        'sm-modal.is_open': False,
        'unsaferules-mode-toggle.value': not curr_unsafemode,
        'sm-modal-title.children': dbc.ModalTitle("Turn on safe mode?") if curr_unsafemode else dbc.ModalTitle("Turn off safe mode?"),
        'sm-modal-body.children': toggle_safe_rules_on_msg if curr_unsafemode else toggle_safe_rules_off_msg,
        'hl-rules.value': str(app_settings.current_mapelites.lsystem.hl_solver.parser.rules),
        'heatmap-plot.figure': _build_heatmap(mapelites=app_settings.current_mapelites,
                                              pop_name=kwargs['pop_name'],
                                              metric_name=kwargs['metric_name'],
                                              method_name=kwargs['method_name']),
        'content-plot.figure': _get_elite_content(mapelites=app_settings.current_mapelites,
                                                  bin_idx=None,
                                                  pop=None),
        'spaceship-properties.children': get_properties_table(cs=None)
    }


# map between components and method
triggers_map = {
    'step-btn': __apply_step,
    'rand-step-btn': __apply_step,
    'reset-btn': __reset,
    'bc0-Major-axis_Medium-axis': __bc_change,
    'bc0-Major-axis_Smallest-axis': __bc_change,
    'bc0-Average-Proportions': __bc_change,
    'bc0-Symmetry': __bc_change,
    'bc1-Major-axis_Medium-axis': __bc_change,
    'bc1-Major-axis_Smallest-axis': __bc_change,
    'bc1-Average-Proportions': __bc_change,
    'bc1-Symmetry': __bc_change,
    'subdivide-btn': __subdivide,
    'lsystem-modules': __lsystem_modules,
    'update-rules-btn': __update_rules,
    'population-feasible': __update_heatmap,
    'population-infeasible': __update_heatmap,
    'metric-fitness': __update_heatmap,
    'metric-age': __update_heatmap,
    'metric-coverage': __update_heatmap,
    'method-radio': __update_heatmap,
    'symmetry-none': __apply_symmetry,
    'symmetry-x': __apply_symmetry,
    'symmetry-z': __apply_symmetry,
    'heatmap-plot': __update_content,
    'population_dropdown': __update_content,
    'selection-btn': __selection,
    'selection-clr-btn': __clear_selection,
    'emitter-human': __emitter,
    'emitter-random': __emitter,
    'emitter-greedy': __emitter,
    'emitter-prefmatrix': __emitter,
    'emitter-prefbandit': __emitter,
    'emitter-conbandit': __emitter,
    'emitter-knn': __emitter,
    'emitter-linkernel': __emitter,
    'emitter-rbfkernel': __emitter,
    'popdownload-btn': __population_download,
    'popupload-data': __population_upload,
    'nbs-err-btn': __close_error,
    'color-picker-btn': __color,
    'fitness-sldr': __fitness_weights,
    'webapp-quickstart-btn': __show_quickstart_modal,
    'voxel-preview-toggle': __toggle_voxelization,
    'unsaferules-mode-toggle': __show_toggle_unsafe_mode_modal,
    'tsrm-y-btn': __toggle_unsafe_mode,
    None: __default
}


@app.callback(Output('heatmap-plot', 'figure'),
              Output('content-plot', 'figure'),
              Output('hl-rules', 'value'),
              Output('selected-bin', 'children'),
              Output('content-string', 'value'),
              Output('spaceship-properties', 'children'),
              Output('step-spinner', 'children'),
              Output("download-population", "data"),
              Output("download-metrics", "data"),
              Output('population-dropdown', 'label'),
              Output('metric-dropdown', 'label'),
              Output('b0-dropdown', 'label'),
              Output('b1-dropdown', 'label'),
              Output('symmetry-dropdown', 'label'),
              Output("quickstart-usermode-modal", "is_open"),
              Output("nbs-err-modal", "is_open"),
              Output("rand-step-btn-div", "style"),
              Output("reset-btn-div", "style"),
              Output('download-btn', 'children'),
              Output('content-legend-div', 'children'),
              Output('emitter-dropdown', 'label'),
              Output('sm-modal', 'is_open'),
              Output("unsaferules-mode-toggle", "value"),
              Output('unsafemode-div', 'style'),
              Output('sm-modal-title', 'children'),
              Output('sm-modal-body', 'children'),
              Output('emitter-steps-div', 'style'),
              Output('symmetry-div', 'style'),
              Output('experiment-settings-div', 'style'),

              State('heatmap-plot', 'figure'),
              State('hl-rules', 'value'),
              State('content-plot', 'figure'),
              State('content-string', 'value'),
              State('spaceship-properties', 'children'),
              State('population-dropdown', 'label'),
              State('metric-dropdown', 'label'),
              State('b0-dropdown', 'label'),
              State('b1-dropdown', 'label'),
              State('symmetry-dropdown', 'label'),
              State('emitter-dropdown', 'label'),
              State("quickstart-usermode-modal", "is_open"),
              State("nbs-err-modal", "is_open"),
              State("rand-step-btn-div", "style"),
              State("reset-btn-div", "style"),
              State('download-btn', 'children'),
              State('content-legend-div', 'children'),
              State('color-picker', 'value'),
              State("content-plot", "relayoutData"),
              State("voxel-preview-toggle", "value"),
              State("unsaferules-mode-toggle", "value"),
              State('unsafemode-div', 'style'),
              State('sm-modal-title', 'children'),
              State('sm-modal-body', 'children'),
              State('emitter-steps-div', 'style'),

              Input('population-feasible', 'n_clicks'),
              Input('population-infeasible', 'n_clicks'),
              Input('metric-fitness', 'n_clicks'),
              Input('metric-age', 'n_clicks'),
              Input('metric-coverage', 'n_clicks'),
              Input('method-radio', 'value'),
              Input('step-btn', 'n_clicks'),
              Input('rand-step-btn', 'n_clicks'),
              Input('reset-btn', 'n_clicks'),
              Input('subdivide-btn', 'n_clicks'),
              Input({'type': 'fitness-sldr', 'index': ALL}, 'value'),
              Input('bc0-Major-axis_Medium-axis', 'n_clicks'),
              Input('bc0-Major-axis_Smallest-axis', 'n_clicks'),
              Input('bc0-Average-Proportions', 'n_clicks'),
              Input('bc0-Symmetry', 'n_clicks'),
              Input('bc1-Major-axis_Medium-axis', 'n_clicks'),
              Input('bc1-Major-axis_Smallest-axis', 'n_clicks'),
              Input('bc1-Average-Proportions', 'n_clicks'),
              Input('bc1-Symmetry', 'n_clicks'),
              Input('lsystem-modules', 'value'),
              Input('update-rules-btn', 'n_clicks'),
              Input('heatmap-plot', 'clickData'),
              Input('selection-btn', 'n_clicks'),
              Input('selection-clr-btn', 'n_clicks'),
              Input('emitter-human', 'n_clicks'),
              Input('emitter-random', 'n_clicks'),
              Input('emitter-greedy', 'n_clicks'),
              Input('emitter-prefmatrix', 'n_clicks'),
              Input('emitter-prefbandit', 'n_clicks'),
              Input('emitter-conbandit', 'n_clicks'),
              Input('emitter-knn', 'n_clicks'),
              Input('emitter-linkernel', 'n_clicks'),
              Input('emitter-rbfkernel', 'n_clicks'),
              Input("download-btn", "n_clicks"),
              Input('popdownload-btn', 'n_clicks'),
              Input('popupload-data', 'filename'),
              Input('symmetry-none', 'n_clicks'),
              Input('symmetry-x', 'n_clicks'),
              Input('symmetry-z', 'n_clicks'),
              Input("nbs-err-btn", "n_clicks"),
              Input('color-picker-btn', 'n_clicks'),
              Input('webapp-quickstart-btn', 'n_clicks'),
              Input("voxel-preview-toggle", "value"),
              Input("unsaferules-mode-toggle", "value"),
              Input("tsrm-y-btn", "n_clicks"),
              )
def general_callback(curr_heatmap: Dict[str, Any],
                     rules: str,
                     curr_content: Dict[str, Any],
                     cs_string: str,
                     cs_properties: List[Any],
                     pop_name: str,
                     metric_name: str,
                     b0: str,
                     b1: str,
                     symm_axis: str,
                     emitter_name: str,
                     qs_um_modal_show: bool,
                     nbs_err_modal_show: bool,
                     rand_step_btn_style: Dict[str, str],
                     reset_btn_style: Dict[str, str],
                     dlbtn_label: str,
                     curr_legend: List[Any],
                     color: str,
                     curr_camera: Dict[str, str],
                     curr_voxel_display: bool,
                     curr_unsafemode: bool,
                     curr_unsafemode_div_style: Dict[str, str],
                     curr_unsafemode_title: str,
                     curr_unsafemode_body: str,
                     curr_emittersteps_style: Dict[str, str],
                     
                     pop_feas: int,
                     pop_infeas: int,
                     metric_fitness: int,
                     metric_age: int,
                     metric_coverage: int,
                     method_name: str,
                     n_clicks_step: int,
                     n_clicks_rand_step: int,
                     n_clicks_reset: int,
                     n_clicks_sub: int,
                     weights: Dict[str, float],
                     b0_mame: int,
                     b0_mami: int,
                     b0_avgp: int,
                     b0_sym: int,
                     b1_mame: int,
                     b1_mami: int,
                     b1_avgp: int,
                     b1_sym: int,
                     modules: bool,
                     n_clicks_rules: int,
                     clickData: Dict[str, Any],
                     selection_btn: int,
                     clear_btn: int,
                     emitter1_nclicks: int,
                     emitter2_nclicks: int,
                     emitter3_nclicks: int,
                     emitter4_nclicks: int,
                     emitter5_nclicks: int,
                     emitter6_nclicks: int,
                     emitter7_nclicks: int,
                     emitter8_nclicks: int,
                     emitter9_nclicks: int,
                     n_clicks_cs_download: int,
                     n_clicks_popdownload: int,
                     upload_filename: str,
                     symm_none: int,
                     symm_x: int,
                     symm_z: int,
                     nbs_btn: int,
                     color_btn: int,
                     qs_btn: int,
                     switch_voxel_display: bool,
                     switch_unsafemode: bool,
                     confirm_unsafemode_switch: int,
                     prevent_initial_call=False) -> Tuple[Any, ...]:
    """General callback for the application.

    Args:
        curr_heatmap (Dict[str, Any]): The current spaceships population heatmap.
        rules (str): The current L-system rules.
        curr_content (Dict[str, Any]): The current spaceship preview plot.
        cs_string (str): The current spaceship string.
        cs_properties (List[Any]): The current spaceship properties.
        pop_name (str): The current population name to display.
        metric_name (str): The current metric name to display.
        b0 (str): The current first behavior characteristic (X axis).
        b1 (str): The current second behavior characteristic (Y axis).
        symm_axis (str): The current symmetry axis.
        emitter_name (str): The current emitter name.
        qs_modal_show (bool): Whether the "Quickstart" modal is currently displayed.
        qs_um_modal_show (bool): Whether the "Quickstart" modal (for the user mode) is currently displayed.
        cm_modal_show (bool): Whether the "Privacy Policy" modal is currently displayed.
        nbs_err_modal_show (bool): Whether the "Warning" modal is currently displayed.
        rand_step_btn_style (Dict[str, str]): The CSS style of the "Evolve from Random Spaceship" button.
        reset_btn_style (Dict[str, str]): The CSS style of the "Reinitialize Population" button.
        dlbtn_label (str): The current label of the spaceship download button.
        curr_legend (List[Any]): The current spaceship preview legend.
        color (str): The currently picked spaceship base blocks color.
        curr_camera (Dict[str, str]): The current spaceship preview plot camera position.
        curr_voxel_display (bool): Whether the spaceship preview plot currently uses voxels.
        pop_feas (int): The number of button clicks.
        pop_infeas (int): The number of button clicks.
        metric_fitness (int): The number of button clicks.
        metric_age (int): The number of button clicks.
        metric_coverage (int): The number of button clicks.
        method_name (str): The new value.
        n_clicks_step (int): The number of button clicks.
        n_clicks_rand_step (int): The number of button clicks.
        n_clicks_reset (int): The number of button clicks.
        n_clicks_sub (int): The number of button clicks.
        weights (Dict[str, float]): The new values.
        b0_mame (int): The number of button clicks.
        b0_mami (int): The number of button clicks.
        b0_avgp (int): The number of button clicks.
        b0_sym (int): The number of button clicks.
        b1_mame (int): The number of button clicks.
        b1_mami (int): The number of button clicks.
        b1_avgp (int): The number of button clicks.
        b1_sym (int): The number of button clicks.
        modules (bool): The new value.
        n_clicks_rules (int): The number of button clicks.
        clickData (Dict[str, Any]): The heatmap click data.
        selection_btn (int): The number of button clicks.
        clear_btn (int): The number of button clicks.
        emitter1_nclicks (int): The number of button clicks.
        emitter2_nclicks (int): The number of button clicks.
        emitter3_nclicks (int): The number of button clicks.
        emitter4_nclicks (int): The number of button clicks.
        emitter5_nclicks (int): The number of button clicks.
        emitter6_nclicks (int): The number of button clicks.
        emitter7_nclicks (int): The number of button clicks.
        emitter8_nclicks (int): The number of button clicks.
        emitter9_nclicks (int): The number of button clicks.
        n_clicks_cs_download (int): The number of button clicks.
        n_clicks_popdownload (int): The number of button clicks.
        upload_filename (str): The filename.
        symm_none (int): The number of button clicks.
        symm_x (int): The number of button clicks.
        symm_y (int): The number of button clicks.
        symm_z (int): The number of button clicks.
        symm_orientation (str): The new value.
        nclicks_yes (int): The number of button clicks.
        nclicks_no (int): The number of button clicks.
        nbs_btn (int): The number of button clicks.
        color_btn (int): The number of button clicks.
        qs_btn (int): The number of button clicks.
        switch_voxel_display (bool): The new value.

    Returns:
        Tuple[Any, ...]: _description_
    """
    global app_settings
    global first_launch

    ctx = dash.callback_context

    if not ctx.triggered:
        event_trig = None
    else:
        event_trig = ctx.triggered[0]['prop_id'].split('.')[0]

    if event_trig is None and first_launch:
        event_trig = 'reset-btn'
        first_launch = False
    
    if event_trig not in triggers_map:
        try:
            import ast
            event_trig = ast.literal_eval(event_trig)
            event_trig = event_trig['type']
        except ValueError:
            logging.getLogger('webapp').error(
                msg=f'[{__name__}.general_callback] Unrecognized {event_trig=}. No operations have been applied!')
    
    vars = locals()    
    
    output = {
        'heatmap-plot.figure': curr_heatmap,
        'content-plot.figure': curr_content,
        'hl-rules.value': rules,
        'selected-bin.children': '',
        'content-string.value': cs_string,
        'spaceship-properties.children': cs_properties,
        'step-spinner.children': '',
        'download-population.data': None,
        'download-metrics.data': None,
        'population-dropdown.label': pop_name,
        'metric-dropdown.label': metric_name,
        'b0-dropdown.label': b0,
        'b1-dropdown.label': b1,
        'symmetry-dropdown.label': symm_axis,
        'quickstart-usermode-modal.is_open': qs_um_modal_show,
        'nbs-err-modal.is_open': nbs_err_modal_show,
        'rand-step-btn-div.style': rand_step_btn_style,
        'reset-btn-div.style': reset_btn_style,
        'download-btn.children': dlbtn_label,
        'content-legend-div.children': curr_legend,
        'emitter-dropdown.label': emitter_name,
        'sm-modal.is_open': False,
        'unsaferules-mode-toggle.value': curr_unsafemode,
        'unsafemode-div.style': curr_unsafemode_div_style,
        'sm-modal-title.children': curr_unsafemode_title,
        'sm-modal-body.children': curr_unsafemode_body,
        'emitter-steps-div.style': curr_emittersteps_style,
        'symmetry-div.style': dash.no_update,
        'experiment-settings-div.style': dash.no_update
    }

    logging.getLogger('webapp').debug(
        f'[{__name__}.general_callback] {event_trig=}; {app_settings.gen_counter=}; {app_settings.selected_bins=}; {app_settings.current_mapelites.emitter=}; {process_semaphore.is_locked=}')

    if not process_semaphore.is_locked:
        process_semaphore.lock(name=event_trig)

        u = triggers_map[event_trig](**vars)
        for k in u.keys():
            output[k] = u[k]

        app_settings.selected_bins, selected_bins_str = _format_bins(mapelites=app_settings.current_mapelites,
                                                                     bins_idx_list=app_settings.selected_bins,
                                                                     do_switch=True,
                                                                     str_prefix='Selected bin(s):',
                                                                     filter_out_empty=True)

        output['selected-bin.children'] = selected_bins_str

        if app_settings.selected_bins and len(curr_content['data']) == 0:
            output['content-plot.figure'] = _get_elite_content(mapelites=app_settings.current_mapelites,
                                                               bin_idx=_switch(
                                                                   [app_settings.selected_bins[-1]])[0],
                                                               pop='feasible',
                                                               camera=curr_camera.get(
                                                                   'scene.camera', None),
                                                               show_voxel=curr_voxel_display)

        process_semaphore.unlock()

    return tuple(output.values())
