import os
import time
import json
from datetime import datetime
from collections import OrderedDict

from pyqtgraph.Qt import QtGui, QtCore
from serial import SerialException
from concurrent.futures import ThreadPoolExecutor

from config.gui_settings import  update_interval
from config.paths import dirs
from com.pycboard import Pycboard, PyboardError
from com.data_logger import Data_logger
from gui.plotting import Experiment_plot
from gui.dialogs import Variables_dialog, Summary_variables_dialog
from gui.utility import variable_constants
from gui.markov_gui.markov_variable_dialog import *
from gui.sequence_gui.sequence_variable_dialog import *
from gui.telegram_notifications import *

class Run_experiment_tab(QtGui.QWidget):
    '''The run experiment tab is responsible for setting up, running and stopping
    an experiment that has been defined using the configure experiments tab.'''

    def __init__(self, parent=None):
        super(QtGui.QWidget, self).__init__(parent)

        self.GUI_main = self.parent()
        self.experiment_plot = Experiment_plot(self)

        self.name_label = QtGui.QLabel('Experiment name:')
        self.name_text  = QtGui.QLineEdit()
        self.name_text.setReadOnly(True)
        self.plots_button =  QtGui.QPushButton('Show plots')
        self.plots_button.setIcon(QtGui.QIcon("gui/icons/bar-graph.svg"))
        self.plots_button.clicked.connect(self.experiment_plot.show)
        self.startstopclose_all_button = QtGui.QPushButton()
        self.startstopclose_all_button.clicked.connect(self.startstopclose_all)

        self.Hlayout = QtGui.QHBoxLayout()
        self.Hlayout.addWidget(self.name_label)
        self.Hlayout.addWidget(self.name_text)
        self.Hlayout.addWidget(self.plots_button)
        self.Hlayout.addWidget(self.startstopclose_all_button)

        self.scroll_area = QtGui.QScrollArea(parent=self)
        # self.scroll_area.horizontalScrollBar().setEnabled(False)
        self.scroll_inner = QtGui.QFrame(self)
        self.boxes_layout = QtGui.QGridLayout(self.scroll_inner)
        self.scroll_area.setWidget(self.scroll_inner)
        self.scroll_area.setWidgetResizable(True)

        self.Vlayout = QtGui.QVBoxLayout(self)
        self.Vlayout.addLayout(self.Hlayout)
        self.Vlayout.addWidget(self.scroll_area)

        self.subjectboxes = []

        self.update_timer = QtCore.QTimer() # Timer to regularly call update() during run.        
        self.update_timer.timeout.connect(self.update)

    # Functions used for multithreaded task setup.

    def thread_map(self, func):
        '''Map func over range(self.n_setups) using seperate threads for each call.
        Used to run experiment setup functions on all boards in parallel. Print 
        output is delayed during multithreaded operations to avoid error message
        when trying to call PyQt method from annother thread.'''
        for subject_box in self.subjectboxes:
            subject_box.start_delayed_print()
        with ThreadPoolExecutor(max_workers=self.n_setups) as executor:
            return_value = executor.map(func, range(self.n_setups))
        for subject_box in self.subjectboxes:
            subject_box.end_delayed_print()
        return return_value

    def connect_to_board(self, i):
        '''Connect to the i-th board.'''
        subject = self.subjects[i]
        setup = self.experiment['subjects'][subject]['setup']
        print_func = self.subjectboxes[i].print_to_log
        serial_port = self.GUI_main.setups_tab.get_port(setup)
        try:
            board = Pycboard(serial_port, print_func=print_func)
        except SerialException:
            print_func('\nConnection failed.')
            self.setup_failed[i] = True
            return
        if not board.status['framework']:
            print_func('\nInstall pyControl framework on board before running experiment.')
            self.setup_failed[i] = True
            self.subjectboxes[i].error()
        board.subject = subject
        board.setup_ID = setup
        return board

    def start_hardware_test(self, i):
        '''Start hardware test on i-th board'''
        try:
            board = self.boards[i]
            board.setup_state_machine(self.experiment['hardware_test'])
            board.start_framework(data_output=False)
            time.sleep(0.01)
            board.process_data()
        except PyboardError:
            self.setup_failed[i] = True
            self.subjectboxes[i].error()

    def setup_task(self, i):
        '''Load the task state machine and set variables on i-th board.'''
        board = self.boards[i]
        # Setup task state machine.
        try:
            board.data_logger = Data_logger(print_func=board.print, data_consumers=
                [self.experiment_plot.subject_plots[i]])
            board.setup_state_machine(self.experiment['task'])
        except PyboardError:
            self.setup_failed[i] = True
            self.subjectboxes[i].error()
            return
        # Set variables.
        board.subject_variables = [v for v in self.experiment['variables'] 
                                   if v['subject'] in ('all', board.subject)]
        if board.subject_variables:
            board.print('\nSetting variables.\n')
            board.variables_set_pre_run = []
            try:
                try:
                    subject_pv_dict = self.persistent_variables[board.subject]
                except KeyError:
                    subject_pv_dict = {}
                for v in board.subject_variables:
                    if v['persistent'] and v['name'] in subject_pv_dict.keys(): # Use stored value.
                        v_value =  subject_pv_dict[v['name']]
                        board.variables_set_pre_run.append(
                            (v['name'], str(v_value), '(persistent value)'))
                    else:
                        if v['value'] == '':
                            continue
                        v_value = eval(v['value'], variable_constants) # Use value from variables table.
                        board.variables_set_pre_run.append((v['name'], v['value'], ''))
                    board.set_variable(v['name'], v_value)
                # Print set variables to log.    
                if board.variables_set_pre_run:
                    name_len  = max([len(v[0]) for v in board.variables_set_pre_run])
                    value_len = max([len(v[1]) for v in board.variables_set_pre_run])
                    for v_name, v_value, pv_str in board.variables_set_pre_run:
                        self.subjectboxes[i].print_to_log(
                            v_name.ljust(name_len+4) + v_value.ljust(value_len+4) + pv_str)
            except PyboardError as e:
                board.print('Setting variable failed. ' + str(e))
                self.setup_failed[i] = True
        return

    # Main setup experiment function.

    def setup_experiment(self, experiment):
        '''Called when an experiment is loaded.'''
        # Setup tabs.
        self.experiment = experiment
        self.GUI_main.tab_widget.setTabEnabled(0, False) # Disable run task tab.
        self.GUI_main.tab_widget.setTabEnabled(2, False)  # Disable setups tab.
        self.GUI_main.experiments_tab.setCurrentWidget(self)
        self.experiment_plot.setup_experiment(experiment)
        self.startstopclose_all_button.setText('Start All')
        self.startstopclose_all_button.setIcon(QtGui.QIcon("gui/icons/play.svg"))
        self.startstopclose_all_button.setStyleSheet("background-color:#68ff66")
        # Setup controls box.
        self.name_text.setText(experiment['name'])
        self.startstopclose_all_button.setEnabled(False)
        self.plots_button.setEnabled(False)
        # Setup Telegram
        telegram_json = self.get_settings_from_json()
        if telegram_json['notifications_on']:
            self.telegrammer = Telegram(self.subjectboxes,telegram_json['bot_token'],telegram_json['chat_id'])
        else:
            self.telegrammer = None
        # Setup subjectboxes
        self.subjects = list(experiment['subjects'].keys())
        self.subjects.sort(key=lambda s: experiment['subjects'][s]['setup'])
        for i,subject in enumerate(self.subjects):
            self.subjectboxes.append(
                Subjectbox('{} --- {}'.format(experiment['subjects'][subject]['setup'], subject), i, self))
            self.boxes_layout.addWidget(self.subjectboxes[-1])
            position = int(experiment['subjects'][subject]['setup'].split('.')[-1])-1
            if position<3:
                row = 0
            else:
                row = 1
            self.boxes_layout.addWidget(self.subjectboxes[-1],row,position-3*row)
        # Create data folder if needed.
        if not os.path.exists(self.experiment['data_dir']):
            os.mkdir(self.experiment['data_dir'])        
        # Load persistent variables if they exist.
        self.pv_path = os.path.join(self.experiment['data_dir'], 'persistent_variables.json')
        if os.path.exists(self.pv_path):
            with open(self.pv_path, 'r') as pv_file:
                self.persistent_variables =  json.loads(pv_file.read())
        else:
            self.persistent_variables = {}
        self.GUI_main.app.processEvents()
        # Setup boards.
        self.print_to_logs('Connecting to board.. ')
        self.n_setups = len(self.subjects)
        self.setup_failed = [False] * self.n_setups # Element i set to True to indicate setup has failed on board i.
        self.boards = [board for board in self.thread_map(self.connect_to_board)]
        if any(self.setup_failed):
            self.abort_experiment()
            return
        # Hardware test.
        if experiment['hardware_test'] != 'no hardware test':
            reply = QtGui.QMessageBox.question(self, 'Hardware test', 'Run hardware test?',
                QtGui.QMessageBox.Yes | QtGui.QMessageBox.No)
            if reply == QtGui.QMessageBox.Yes:
                self.print_to_logs('\nStarting hardware test.')
                self.thread_map(self.start_hardware_test)
                if any(self.setup_failed):
                    self.abort_experiment()
                    return
                QtGui.QMessageBox.question(self, 'Hardware test', 
                    'Press OK when finished with hardware test.', QtGui.QMessageBox.Ok)
                for i, board in enumerate(self.boards):
                    try:
                        board.stop_framework()
                        time.sleep(0.05)
                        board.process_data()
                    except PyboardError as e:
                        self.setup_failed[i] = True
                        board.print('\n' + str(e))
                        self.subjectboxes[i].error()
                if any(self.setup_failed):
                    self.abort_experiment()
                    return
        # Setup task
        self.print_to_logs('\nSetting up task.')
        self.thread_map(self.setup_task)
        if any(self.setup_failed):
            self.abort_experiment()
            return
        # Copy task file to experiments data folder.
        self.boards[0].data_logger.copy_task_file(self.experiment['data_dir'], dirs['tasks'])
        # Configure GUI ready to run.
        for i, board in enumerate(self.boards):
            self.subjectboxes[i].assign_board(board)
            self.subjectboxes[i].start_stop_button.setEnabled(True)
            self.subjectboxes[i].switch_view()
            self.subjectboxes[i].start_stop_button.setText('Start')
            self.subjectboxes[i].start_stop_button.setIcon(QtGui.QIcon("gui/icons/play.svg"))
            self.subjectboxes[i].start_stop_button.setStyleSheet("background-color:#68ff66;")
        self.experiment_plot.set_state_machine(board.sm_info)
        self.startstopclose_all_button.setEnabled(True)
        self.plots_button.setEnabled(True)
        self.setups_started  = 0
        self.setups_finished = 0

    def startstopclose_all(self):
        '''Called when startstopclose_all_button is clicked.  Button is 
        only active if all setups are in the same state.'''
        if self.startstopclose_all_button.text() == 'Close Experiment':
            self.close_experiment()
        elif self.startstopclose_all_button.text() == 'Start All':
            for i, board in enumerate(self.boards):
                self.subjectboxes[i].start_task()
        elif self.startstopclose_all_button.text() == 'Stop All':
            for i, board in enumerate(self.boards):
                self.subjectboxes[i].stop_task()

    def update_startstopclose_button(self):
        '''Called when a setup is started or stopped to update the
        startstopclose_all button.'''
        if self.setups_finished == len(self.boards):
            self.startstopclose_all_button.setText('Close Experiment')
            self.startstopclose_all_button.setIcon(QtGui.QIcon("gui/icons/close.svg"))
            self.startstopclose_all_button.setStyleSheet("background-color:none;")
        else:
            self.startstopclose_all_button.setText('Stop All')
            self.startstopclose_all_button.setIcon(QtGui.QIcon("gui/icons/stop.svg"))
            if self.setups_started == len(self.boards) and self.setups_finished == 0:
                self.startstopclose_all_button.setEnabled(True)
                self.startstopclose_all_button.setStyleSheet("background-color:#ff6666;")
            else:
                self.startstopclose_all_button.setEnabled(False)
                self.startstopclose_all_button.setStyleSheet("background-color:none;")

    def stop_experiment(self):
        self.update_timer.stop()
        self.GUI_main.refresh_timer.start(self.GUI_main.refresh_interval)
        for i, board in enumerate(self.boards):
            time.sleep(0.05)
            board.process_data()
        # Summary and persistent variables.
        summary_variables = [v for v in self.experiment['variables'] if v['summary']]
        sv_dict = OrderedDict()
        if os.path.exists(self.pv_path):
            with open(self.pv_path, 'r') as pv_file:
                persistent_variables = json.loads(pv_file.read())
        else:
            persistent_variables = {}
        for i, board in enumerate(self.boards):
            #  Store persistent variables.
            subject_pvs = [v for v in board.subject_variables if v['persistent']]
            if subject_pvs:
                board.print('\nStoring persistent variables.')
                persistent_variables[board.subject] = {
                    v['name']: board.get_variable(v['name']) for v in subject_pvs}
            # Read summary variables.
            if summary_variables:
                sv_dict[board.subject] = {v['name']: board.get_variable(v['name'])
                                          for v in summary_variables}
                for v_name, v_value in sv_dict[board.subject].items():
                    board.data_logger.data_file.write('\nV -1 {} {}'.format(v_name, v_value))
                    board.data_logger.data_file.flush()
        if persistent_variables:
            with open(self.pv_path, 'w') as pv_file:
                pv_file.write(json.dumps(persistent_variables, sort_keys=True, indent=4))
        if summary_variables:
            Summary_variables_dialog(self, sv_dict).show()
        self.startstopclose_all_button.setEnabled(True)

    def abort_experiment(self):
        '''Called if an error occurs while the experiment is being set up.'''
        self.update_timer.stop()
        self.GUI_main.refresh_timer.start(self.GUI_main.refresh_interval)
        for i, board in enumerate(self.boards):
            # Stop running boards.
            if board and board.framework_running:
                board.stop_framework()
                time.sleep(0.05)
                board.process_data()
                self.subjectboxes[i].stop_task()
        msg = QtGui.QMessageBox()
        msg.setWindowTitle('Error')
        msg.setText('An error occured while setting up experiment')
        msg.setIcon(QtGui.QMessageBox.Warning)
        msg.exec()
        self.startstopclose_all_button.setText('Close Experiment')
        self.startstopclose_all_button.setIcon(QtGui.QIcon("gui/icons/close.svg"))
        self.startstopclose_all_button.setStyleSheet("background-color:none;")
        self.startstopclose_all_button.setEnabled(True)

    def close_experiment(self):
        self.GUI_main.tab_widget.setTabEnabled(0, True) # Enable run task tab.
        self.GUI_main.tab_widget.setTabEnabled(2, True) # Enable setups tab.
        self.GUI_main.experiments_tab.setCurrentWidget(self.GUI_main.configure_experiment_tab)
        self.experiment_plot.close_experiment()
        # Close boards.
        for board in self.boards:
            if board.data_logger: board.data_logger.close_files()
            board.close()
        # Clear subjectboxes.
        while len(self.subjectboxes) > 0:
            subjectbox = self.subjectboxes.pop() 
            subjectbox.setParent(None)
            subjectbox.deleteLater()
        
        if self.telegrammer:
            self.telegrammer.updater.stop()

    def update(self):
        '''Called regularly while experiment is running'''
        for subjectbox in self.subjectboxes:
            subjectbox.update()
        self.experiment_plot.update()
        if self.setups_finished == len(self.boards):
            self.stop_experiment()

    def print_to_logs(self, print_str):
        '''Print to all subjectbox logs.'''
        for subjectbox in self.subjectboxes:
            subjectbox.print_to_log(print_str)

    def get_settings_from_json(self):
        json_path = os.path.join(dirs['config'],'telegram.json')
        if os.path.exists(json_path):
            with open(json_path,'r') as f:
                telegram_settings = json.loads(f.read())
        else:
            telegram_settings = {} # missing json file
        return telegram_settings
# -----------------------------------------------------------------------------

class Subjectbox(QtGui.QGroupBox):
    '''Groupbox for displaying data from a single subject.'''

    def __init__(self, name, setup_number, parent=None):

        super(QtGui.QGroupBox, self).__init__("", parent=parent)
        self.board = None # Overwritten with board once instantiated.
        self.GUI_main = self.parent().GUI_main
        self.run_exp_tab = self.parent()
        self.state = 'pre_run'
        self.setup_number = setup_number
        self.parent_telegram = self.parent().telegrammer
        self.print_queue = []
        self.delay_printing = False

        self.boxTitle = QtGui.QLabel(name)
        self.boxTitle.setStyleSheet("font:16pt;color:blue;")

        self.start_stop_button = QtGui.QPushButton('Loading task...')
        self.start_stop_button.setEnabled(False)
        self.time_label = QtGui.QLabel('Time:')
        self.time_text = QtGui.QLineEdit()
        self.time_text.setReadOnly(True)
        self.time_text.setFixedWidth(60)
        self.variables_button = QtGui.QPushButton('Show Variables')
        self.variables_button.setEnabled(False)
        self.log_textbox = QtGui.QTextEdit()
        self.log_textbox.setMinimumHeight(180)
        self.log_textbox.setMinimumWidth(500)
        self.log_textbox.setFont(QtGui.QFont('Courier', 9))
        self.log_textbox.setReadOnly(True)

        self.subjectGridLayout = QtGui.QGridLayout(self)
        self.subjectHeaderLayout = QtGui.QGridLayout()
        self.subjectHeaderLayout.addWidget(self.boxTitle,0,1)
        self.subjectHeaderLayout.addWidget(self.time_label,0,2,QtCore.Qt.AlignRight)
        self.subjectHeaderLayout.addWidget(self.time_text,0,3,QtCore.Qt.AlignLeft)
        self.subjectHeaderLayout.addWidget(self.variables_button,0,4)
        self.subjectHeaderLayout.addWidget(self.start_stop_button,0,5)
        self.subjectHeaderLayout.setColumnStretch(0,1)
        self.subjectHeaderLayout.setColumnStretch(6,1)
        self.subjectGridLayout.addLayout(self.subjectHeaderLayout,0,0,1,2)
        self.subjectGridLayout.addWidget(self.log_textbox,1,0,1,2)
        
    def print_to_log(self, print_string, end='\n'):
        if self.delay_printing:
            self.print_queue.append((print_string, end)) 
            return
        self.log_textbox.moveCursor(QtGui.QTextCursor.End)
        self.log_textbox.insertPlainText(print_string+end)
        self.log_textbox.moveCursor(QtGui.QTextCursor.End)
        self.GUI_main.app.processEvents()

    def start_delayed_print(self):
        '''Store print output to display later to avoid error 
        message when calling print_to_log from different thread.'''
        self.print_queue = []
        self.delay_printing = True

    def end_delayed_print(self):
        self.delay_printing = False
        for p in self.print_queue:
            self.print_to_log(*p)

    def assign_board(self, board):
        self.board = board
        if self.board.sm_info['name'] == 'markov':
            self.variables_dialog = Markov_Variables_dialog(self, self.board)
        elif self.board.sm_info['name'] == 'sequence':
            self.variables_dialog = Sequence_Variables_dialog(self, self.board)
        else:
            self.variables_dialog = Variables_dialog(self, self.board)
        self.board.data_logger.data_consumers.append(self.variables_dialog)
        self.variables_box= QtGui.QWidget()
        self.variables_box.setLayout(self.variables_dialog.layout)
        self.subjectGridLayout.addWidget(self.variables_box,2,0,1,2,QtCore.Qt.AlignHCenter)
        self.vars_visible = False
        self.variables_box.setVisible(self.vars_visible)
        self.variables_button.clicked.connect(self.switch_view)
        self.variables_button.setEnabled(True)
        self.start_stop_button.clicked.connect(self.start_stop_task)
    
    def start_stop_task(self):
        '''Called when start/stop button on Subjectbox pressed or
        startstopclose_all button is pressed.'''
        if self.state == 'pre_run': 
            self.start_task()
        elif self.state == 'running':
            self.stop_task()

    def start_task(self):
        '''Start the task running on the Subjectbox's board.'''
        self.state = 'running'
        self.run_exp_tab.experiment_plot.start_experiment(self.setup_number)
        self.start_time = datetime.now()
        ex = self.run_exp_tab.experiment
        board = self.board
        board.print('\nStarting experiment.\n')
        board.data_logger.open_data_file(ex['data_dir'], ex['name'], board.setup_ID, board.subject, datetime.now())
        if board.subject_variables: # Write variables set pre run to data file.
            for v_name, v_value, pv in self.board.variables_set_pre_run:
                board.data_logger.data_file.write('V 0 {} {}\n'.format(v_name, v_value))
        board.data_logger.data_file.write('\n')
        board.start_framework()

        self.start_stop_button.setText('Stop')
        self.start_stop_button.setIcon(QtGui.QIcon("gui/icons/stop.svg"))
        self.start_stop_button.setStyleSheet("background-color:#ff6666;")
        self.run_exp_tab.setups_started += 1

        self.run_exp_tab.GUI_main.refresh_timer.stop()
        self.run_exp_tab.update_timer.start(update_interval)
        self.run_exp_tab.update_startstopclose_button()

        if self.parent_telegram:
            self.parent_telegram.add_button(self.setup_number,self.boxTitle)

    def error(self):
        pass

    def stop_task(self,stopped_by_task=False):
        '''Called to stop task or if task stops automatically.'''
        if self.board.framework_running:
            self.board.stop_framework()
        self.start_stop_button.setEnabled(False)
        self.start_stop_button.setStyleSheet("background-color:none;")
        self.run_exp_tab.experiment_plot.active_plots.remove(self.setup_number)
        self.run_exp_tab.setups_finished += 1
        self.run_exp_tab.update_startstopclose_button()
        self.boxTitle.setStyleSheet("font:16pt;color:grey;")
        self.variables_box.setEnabled(False)
        self.vars_visible = True
        self.switch_view()

        if self.parent_telegram:
            if stopped_by_task:
                self.parent_telegram.notify(
                    "<u><b>{}</b></u>\n\nSyringe empty, task stopped\nSession duration= {}".format(self.boxTitle.text(),self.time_text.text())
                )
            self.parent_telegram.remove_button(self.setup_number)

    def update(self):
        '''Called regularly while experiment is running.'''
        if self.board.framework_running:
            try:
                self.board.process_data()
                if not self.board.framework_running:
                    self.stop_task(stopped_by_task=True)
                self.time_text.setText(str(datetime.now()-self.start_time).split('.')[0])
            except PyboardError:
                self.stop_task()
                self.error()

    def switch_view(self):
        '''Switch between viewing data log and variables.'''
        self.vars_visible = not self.vars_visible
        if self.vars_visible:
            self.variables_button.setText('Show Data Log')
        else:
            self.variables_button.setText('Show Variables')
        self.variables_box.setVisible(self.vars_visible)
        self.log_textbox.setVisible(not self.vars_visible)