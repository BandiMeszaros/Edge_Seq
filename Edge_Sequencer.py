import Pyro4 # for python remote objects
import rticonnextdds_connector as rti # for DDS connection
import time # for the sleep method
import threading # for running mutliple threads
from xmltodict import parse
import logging
import queue
import enum
from config import *
import datetime
import re
from subprocess import check_output


def get_local_IP():
    ip_config = check_output('ipconfig').decode()
    ip = set(re.findall('192\.168\.[0-1]\.[\d]*', ip_config)).difference({f'192.168.{i}.{j}'\
                                                                    for i in '01' for j in ['0', '1', '254', '255']})

    return list(ip)[0]

class MSCD_mode(enum.Enum):

    Init = 0
    init_valve = 1
    init_plunger = 2
    Withdraw = 3
    Dispense = 4
    Operate = 5
    Terminate = 6
    Status = 7


class commandPriority(enum.Enum):

    normal = 0
    high = 1


class actionKind(enum.Enum):

    direct_control = 0
    task_control = 1
    stop = 2
    emergency = 3
    acknowledge = 4


class ProcessStatus(enum.Enum):

    none = 0
    sent = 1
    processed = 2
    discarded = 3
    timedout = 4
    lost = 5
    trigger = 6
    hello = 7
    goodbye = 8


class moduleNotFoundError(Exception):

    def __init__(self, moduleID, module_list_file):

        self.moduleID = moduleID
        self.module_list_file = module_list_file

    def __str__(self):
        return f"module {self.moduleID} not found in {self.module_list_file}"


class directCommandError(Exception):
    def __init__(self,task, command):

        self.command = command
        self.task = task
        self.occured = datetime.datetime.now()


    def __str__(self):
        return f"Direct control command didn't finnish in time\n" \
               f"command: {self.command.transactionID}\n" \
               f"task start trigger: {self.task.start_trigger}\n"\
               f"moduleID {command.moduleID}\n" \
               f"Error happend {self.occured}"


class ddsTopicError(Exception):

    def __init__(self, module, command_xml):
        self.module = module
        self.command_xml = command_xml

    def __str__(self):
        return f"Cannot find writer topic in {self.command_xml} for module: {self.module.moduleID}," \
               f" {self.module.uniqueID}"


class Edge_Sequencer:

    def __init__(self, module_list_xml=MODULELIST, config_name_sub=CONFIG_SUB, config_name_pub=CONFIG_PUB,
                 command_xml=COMMANDXML, flow_xml=FLOWXML, es_fct_topic=ES_FCT, timeout_direct_control = DC_TIMEOUT):

        self._module_list_xml = module_list_xml
        self._module_list_dict = self._parse_module_xml(module_list_xml)

        self._instrument = self._module_list_dict["Modulelist"]["Instrument"]
        self._instrumentID = self._module_list_dict["Modulelist"]["InstrumentID"]
        self._config_name_sub = config_name_sub
        self._config_name_pub = config_name_pub
        self._command_xml = command_xml
        self._flow_xml = flow_xml
        self._es_fct_topic = es_fct_topic
        self._directControl_timeout = timeout_direct_control
        #connectors:

        self._fct_read_connector = rti.Connector(config_name=config_name_sub, url=flow_xml)
        self._msct_write_connector = rti.Connector(config_name=config_name_pub, url=command_xml)
        self._fct_write_connector = rti.Connector(config_name=config_name_pub, url=flow_xml)

        if type(self._module_list_dict["Modulelist"]["Module"]) == list:
            self._module_list = [Module(module_dict) for module_dict in self._module_list_dict["Modulelist"]["Module"]]
        else:
            self._module_list = [Module(self._module_list_dict["Modulelist"]["Module"])]

    def _parse_module_xml(self, module_list_xml: str) -> dict:
        """Parses the given xml file, returns the content of the file in an OrderDict"""

        with open(module_list_xml, "r") as file:
            xml_dict = parse(file.read())

        return xml_dict

    def get_instrument(self):
        return self._instrument

    def get_instrumentID(self):
        return self._instrumentID

    def get_module_list(self):
        return self._module_list

    def get_module_list_file_name(self):
        return self._module_list_xml

    def get_flow_xml(self):
        return self._flow_xml

    def get_config_name_sub(self):
        return self._config_name_sub

    def get_config_name_pub(self):
        return self._config_name_pub

    def get_command_xml(self):
        return self._command_xml

    def get_direct_control_timeout(self):
        return self._directControl_timeout

    def get_es_fct_topic(self):
        return self._es_fct_topic

    #todo connector getters
    def get_fct_read_connector(self):
        return self._fct_read_connector

    def get_fct_write_connector(self):
        return self._fct_write_connector

    def get_msct_write_connector(self):
        return self._msct_write_connector

    def _closing_connectors(self):

        self._fct_read_connector.close()
        self._fct_write_connector.close()
        self._msct_write_connector.close()
        logging.debug(f"{datetime.datetime.now()}-> Connectors have been closed....")


@Pyro4.expose
@Pyro4.behavior(instance_mode="single")
class Session(Edge_Sequencer):

    def __init__(self, tasklist_xml: str):
        super().__init__()

        self.start_up_barrier = threading.Barrier(2)
        self.start_up_barrier.reset()

        #todo probably deprecated
        #self.wait_for_read_ack_msct = threading.Event()
        #self.wait_for_read_ack_msct.clear()

        self.xml_task_dict = self._parse_xml_to_dict(tasklist_xml)
        self.tasks = []
        self.running_tasks = []
        self.finished_tasks = []
        self._task_create_from_dict()
        self.num_tasks = len(self.tasks)

        self.trigger_queue = queue.Queue()
        self.queue_lock = threading.Lock()
        self.producer_thread = threading.Thread(name="producer_ESeq",
                                                target=self._producer_thread_target,
                                                args=())
        self.consumer_thread = threading.Thread(target=self._consumer_thread_target, name="consumer_ESeq")
        self.fct_read_write_event = threading.Event()
        self.running_tasks_lock = threading.Lock()
        self.immadiate_stop_flag = threading.Event()
        self.immadiate_stop_flag.set()
        #self.consumer_producer_flag = threading.Event()
        #self.consumer_producer_flag.clear()
        self._kill_all_threads = threading.Event()
        self._kill_all_threads.set()
        self.pyro_thread = threading.Thread(target=self._expose_session, args=(), name="pyro_thread")
        self.pyro_thread.start()

    def _expose_session(self):

        #daemon = Pyro4.Daemon(host=get_local_IP())
        #ns = Pyro4.Proxy('PYRO:Pyro.NameServer@192.168.1.187:9090')

        #for test purposses
        daemon = Pyro4.Daemon(host="localhost")
        ns = Pyro4.locateNS()

        uri = daemon.register(self)
        ns.register("Sequencer.Session", uri)
        self.start_up_barrier.wait()
        logging.debug(f"{datetime.datetime.now()}-> Pyro deamon is running")
        daemon.requestLoop()

    def _task_create_from_dict(self):
        """itterates over the self.xml_data_dict and creates the tasks of the Session"""

        if type(self.xml_task_dict["Tasks"]["Task"]) != list:
            task = self.xml_task_dict["Tasks"]["Task"]
            self.tasks.append(Task(task))
        else:
            for task in self.xml_task_dict["Tasks"]["Task"]:
                self.tasks.append(Task(task))

    def _parse_xml_to_dict(self, xml_file: str) -> dict:
        """Parses the given xml file, returns the content of the file in an OrderDict"""

        with open(xml_file, "r") as file:
            xml_dict = parse(file.read())

        return xml_dict

    def _read_fct(self):
        flow_xml = self.get_flow_xml()
        es_fct_topic = self.get_es_fct_topic()
        read_input = self.read_one_dds_fct(es_fct_topic, flow_xml)
        if read_input != -1:
            return read_input

    def _update_queue_with_new_trigger(self, trigger):
        """puts new items into the list of trigger queue"""
        with self.queue_lock:
            #self.consumer_producer_flag.clear()
            self.trigger_queue.put(trigger)

    def _producer_thread_target(self):
        """runs the _read_fct method, and parses the information in it. Updates the trigger_queue if needed"""
        while self._kill_all_threads.is_set():

            self.immadiate_stop_flag.wait()
            read_data = self._read_fct()
            if read_data["Process_status"] == ProcessStatus.trigger.value:
                if read_data['triggerID'] not in list(self.trigger_queue.queue):
                    self._update_queue_with_new_trigger(read_data["triggerID"])
                    logging.debug(f"{datetime.datetime.now()}-> Producer updates queue with:"
                                  f" {read_data['triggerID']}")

        return 0

    def _consumer_thread_target(self):
        """it is the trigger_queue consumer, reads queue and if matches with any of the triggers
        it updates the corresponding task list"""

        command_xml = self.get_command_xml()
        while self._kill_all_threads.is_set():

            self.immadiate_stop_flag.wait()
            if not self.trigger_queue.empty():
                with self.queue_lock:
                    #self.consumer_producer_flag.set()
                    trigger = self.trigger_queue.get()
                    logging.debug(f"{datetime.datetime.now()}-> consumer has a trigger: {trigger}")

                if self._check_triggers_stop(trigger):
                    task_index = self._return_index_of_triggered_running_task(trigger)
                    self._put_task_to_finnish(task_index)
                    logging.debug(
                        f"{datetime.datetime.now()}-> task put to finnish with stop trigger"
                        f" {trigger} by consumer")

                if self._check_triggers_start(trigger):
                    task_index = self._return_index_of_triggered_idle_task(trigger)
                    logging.debug(
                        f"{datetime.datetime.now()}-> task put to run with start trigger {trigger} by consumer")

                    task = self.tasks[task_index]
                    self._put_task_to_running(task_index)
                    self._send_out_whole_task_to_msct(task, command_xml)

                logging.debug(f"{datetime.datetime.now()}-> finnished tasks:{self.finished_tasks}")
                logging.debug(f"{datetime.datetime.now()}-> tasks: {self.tasks}")
                logging.debug(f"{datetime.datetime.now()}-> running tasks {self.running_tasks}")
        return 0

    def _get_task_triggers(self, task_type: str) -> list:
        """
        It takes one parameter, returns the task triggers based on the task_type argument.
        It returns either the running_tasks triggers, or the triggers of the tasks not yet started.
        IF "running" is given it returns stop triggers, if "idle" it returns start triggers
        :param task_type: A string, it can be 'running', 'idle'
        :return: list of triggers
        """

        if task_type == "running":
            return [task.stop_trigger for task in self.running_tasks]
        elif task_type == "idle":
            return [task.start_trigger for task in self.tasks]

    def _check_triggers_stop(self, trigger):
        """ checks, if recieved trigger is a stop trigger"""
        if trigger in self._get_task_triggers("running"):
            return True
        else:
            return False

    def _check_triggers_start(self, trigger):
        """checks if recieved trigger is a start trigger"""

        if trigger in self._get_task_triggers("idle"):
            return True
        else:
            return False

    def _put_task_to_finnish(self, task_index: int):
        """Task is deleted from the running_tasks list and appended to the finnish list"""
        self.finished_tasks.append(self.running_tasks.pop(task_index))

    def _return_index_of_triggered_running_task(self, trigger: int):
        """It returns the index of the triggered task from the running_tasks list"""

        return self._get_task_triggers("running").index(trigger)

    def _return_index_of_triggered_idle_task(self, trigger: int):
        """It returns the index of the triggered tasks from the tasks list"""

        return self._get_task_triggers("idle").index(trigger)

    def _put_task_to_running(self, task_index: int):
        """Task is deleted from the tasks list and appended to the running_tasks"""
        task = self.tasks.pop(task_index)
        with self.running_tasks_lock:
            self.running_tasks.append(task)

    def _write_fct_trigger(self, task):

        flow_xml = self.get_flow_xml()
        es_flow_topic = self.get_es_fct_topic()
        sequencer_instrumentID = self.get_instrumentID()
        self._write_dds_fct_trigger(es_flow_topic, task, sequencer_instrumentID, flow_xml)

    def client_pyro_update_command_status(self, transactionID):
        """this is a method for the Edge_Client, it is what the client uses to update the completed """
        logging.debug(f"{datetime.datetime.now()}-> Client connected to Sequencer through pyro")
        for task in self.running_tasks:
            if transactionID in task.list_of_command_transactionIds():
                task.put_command_to_done(transactionID)
                logging.debug(f"{datetime.datetime.now()}-> Client updated tasklist through pyro")

    def session_run(self):

        self.start_up_barrier.wait()
        self.consumer_thread.start()
        self.producer_thread.start()


        logging.debug(f"{datetime.datetime.now()}-> finnished tasks:{self.finished_tasks}")
        logging.debug(f"{datetime.datetime.now()}-> tasks: {self.tasks}")
        logging.debug(f"{datetime.datetime.now()}-> running tasks {self.running_tasks}")
        logging.debug(f"{datetime.datetime.now()}-> session started with {len(self.tasks)} tasks")

        while self._kill_all_threads.is_set():
            while len(self.finished_tasks) != self.num_tasks:
                self.immadiate_stop_flag.wait()
                with self.running_tasks_lock:
                    for task in self.running_tasks:
                        if len(task.done_commands) == task.num_commands:
                            logging.debug(f"{datetime.datetime.now()}-> finnished tasks:{self.finished_tasks}")
                            logging.debug(f"{datetime.datetime.now()}-> tasks: {self.tasks}")
                            logging.debug(f"{datetime.datetime.now()}-> running tasks {self.running_tasks}")
                            logging.debug(f"{datetime.datetime.now()}-> There is a task which is done: {task}")
                            if task.waitMode == "WaitAfter":
                                logging.debug(f"{datetime.datetime.now()}"
                                              f"-> WaitAfter task, waiting for {task.waitTime}s")
                                time.sleep(task.waitTime)
                                self._write_fct_trigger(task)
                            else:
                                self._write_fct_trigger(task)
            logging.debug(f"{datetime.datetime.now()}-> No task left...")
            logging.debug(f"{datetime.datetime.now()}-> finnished tasks length: {len(self.finished_tasks)}")
            logging.debug(f"{datetime.datetime.now()}-> finnished tasks:{self.finished_tasks}")
            logging.debug(f"{datetime.datetime.now()}-> tasks: {self.tasks}")
            logging.debug(f"{datetime.datetime.now()}-> running tasks {self.running_tasks}")

            try:
                self._kill_all_threads.clear()
                self._closing_connectors()
            except Exception as err:
                logging.debug(f"{err}")

    def _check_modules(self, command):

        module_list = self.get_module_list()
        moduleIDs = [module.moduleID for module in module_list]

        if command.moduleID in moduleIDs:
            index_module = moduleIDs.index(command.moduleID)
            return module_list[index_module]
        else:
            raise moduleNotFoundError(command.moduleID, self.get_module_list_file_name())

    def _send_out_whole_task_to_msct(self, task, command_xml: str):
        """it sends out every command in the given task"""

        direct_control_timeout = self.get_direct_control_timeout()
        if task.waitMode == "WaitBefore":
            logging.debug(f"{datetime.datetime.now()}-> WaitBefore task, waiting for {task.waitTime}s")
            time.sleep(task.waitTime)

        for command in task.all_commands:
            # todo probably deprecated part, the action kind is evaluated at the client side
            # try:
            #     if command.action_kind == actionKind.task_control:
            #
            #         module = self._check_modules(command)
            #         self.write_command_topic(command, module, command_xml)
            #
            #     elif command.action_kind == actionKind.direct_control:
            #
            #         module = self._check_modules(command)
            #         self.write_command_topic(command, module, command_xml)
            #         direct_command_returned = command.command_done_flag.wait(timeout=direct_control_timeout)
            #         if direct_command_returned:
            #             command.command_done_flag.clear()
            #         else:
            #             raise directCommandError(task, command)

            #it always takes the first element, the Client pops the command element from the list
            module = self._check_modules(command)
            try:
                self.write_command_topic(command, module, command_xml)

            except directCommandError as err:
                self.immadiate_stop_flag.clear()
                logging.debug(f"{datetime.datetime.now()}--> {err}")
                continue_flag = input("Direct Control Command haven't returned in time."
                                      "Continue anyway? If no, Edge_Sequencer stops.(y/n)\n")
                if continue_flag == "y" or continue_flag == "y\n":
                    self.immadiate_stop_flag.set()
                    command.command_done_flag.clear()
                else:
                    # self._kill_all_threads is dangerous, it stops all the threads (producer, consumer, main)
                    logging.debug("Killing all threads....")
                    self._kill_all_threads.clear()

    def _parse_command_xml_command_topic(self, command_xml):

        data_reader = {}
        data_writer = {}
        command_dict = self._load_xml(command_xml)

        domain_participant = command_dict["dds"]['domain_participant_library']['domain_participant']



        for element in domain_participant:

            if "subscriber" in element.keys():
                try:
                    data_reader.update({element["subscriber"]['@name']: element["subscriber"]["data_reader"]})
                except:
                    for subscriber in element["subscriber"]:
                        data_reader.update({subscriber['@name']: subscriber['data_reader']})

            if "publisher" in element.keys():
                try:
                    data_writer.update({element["publisher"]["@name"]: element["publisher"]["data_writer"]})
                except:
                    for publisher in element["publisher"]:
                        data_writer.update({publisher['@name']: publisher['data_writer']})




        #print(data_reader)
        #print(data_writer)

        return {"data_writers": data_writer, "data_readers": data_reader}

    def _load_xml(self, command_xml):
        with open(command_xml, "r") as file:
            xml_dict = parse(file.read())
        return xml_dict

    def _check_rti_connector_publisher_command_topic(self, module, command_xml):

        writers = self._parse_command_xml_command_topic(command_xml)["data_writers"]

        if f"{module.uniqueID}Pub" in [writer_key for writer_key in writers.keys()]:
            pass
        else:
            raise ddsTopicError(module, command_xml)

    def write_command_topic(self, command, module, command_xml):

        try:
            self._check_rti_connector_publisher_command_topic(module, command_xml)
        except ddsTopicError as err:
            logging.critical(f"{err}")
            return 1

        else:
            connector = self.get_msct_write_connector()
            while True:
                try:

                    output = connector.get_output(f"{module.uniqueID}Pub::MyWriter{module.uniqueID}")
                    data_dict = self._create_writer_data_for_module_command_topic(module, command)

                    logging.debug(f"Matched subs:{len(output.matched_subscriptions)}")
                    if len(output.matched_subscriptions) == 0:
                        logging.debug(f"Matched subs:{len(output.matched_subscriptions)}")
                        output.wait_for_subscriptions()

                    output.instance.set_dictionary(data_dict)
                    logging.debug(f"{datetime.datetime.now()}-> sending out command: {command.transactionID}")
                    output.write()
                    #todo probably deprecated
                    #self.wait_for_read_ack_msct.wait()
                    #output.clear_members()
                    #self.wait_for_read_ack_msct.clear()

                except Exception as err:
                    logging.critical(err)
                    logging.critical("error happend while writing command topic")
                    continue
                else:
                    break
            return 0


#todo: probably deprecated
    # def pyro_msct_ack_set(self):
    #     self.wait_for_read_ack_msct.set()

    def _create_writer_data_for_module_command_topic(self, module, command):
        """creates a data structure that is compatable with the dds topic"""
        module_uid = module.uniqueID
        command_transaction_id = command.transactionID
        command_mscd = command.mscd
        priority = command.priority.value
        action_kind = command.action_kind.value
        command_sessionID = command.sessionID
        data_dict = {"moduleID": module.moduleID,"GCD": {"transactionID": command_transaction_id,"Priority": priority,
                                                         "Action_kind": action_kind,
                                                         "sessionID": command_sessionID},
                     f"MSCD {module_uid}": command_mscd}
        return data_dict

    def read_one_dds_fct(self, es_flow_topic, flow_xml):
        try:
            self._check_rti_connector_subscriber_flow_topic(es_flow_topic, flow_xml)
        except ddsTopicError as err:
            logging.critical(f"{err}")
            return -1

        else:
            return_dictionary = {}
            connector = self.get_fct_read_connector()
        while True:
            try:
                input_connector = connector.get_input(f"{es_flow_topic}Sub::MyReader{es_flow_topic}")
                self._fct_read_connector.wait()  # waiting for data
                input_connector.take()  # taking in the data
                logging.debug(f"{datetime.datetime.now()}-> Input taken on fct")

                for sample in input_connector.samples.valid_data_iter:
                    return_dictionary.update(sample.get_dictionary())
            except:
                continue
            else:
                self.fct_read_write_event.set()
                return return_dictionary

    def _write_dds_fct_trigger(self, es_flow_topic, task, sequencer_instrumentID, flow_xml):
        try:
            self._check_rti_connector_publisher_flow_topic(es_flow_topic, flow_xml)
        except ddsTopicError as err:
            logging.critical(f"{err}")
            return 1

        else:
            self.fct_read_write_event.clear()
            connector = self.get_fct_write_connector()
            while True:
                try:
                    output = connector.get_output(f"{es_flow_topic}Pub::MyWriter{es_flow_topic}")

                    if len(output.matched_subscriptions) == 0:
                        output.wait_for_subscriptions()

                    data_dict = self._create_writer_data_for_flow_topic(task, sequencer_instrumentID)
                    output.instance.set_dictionary(data_dict)
                    output.write()
                    logging.debug(f"{datetime.datetime.now()}-> data have been sent out on fct: {task}")
                    self.fct_read_write_event.wait()
                    output.clear_members()
                except:
                    logging.critical("Error happend while writing dds_fct")
                    continue
                else:
                    return 0

    def _create_writer_data_for_flow_topic(self, task, instrumentID):
        """creates a data structure that is compatable with the dds topic"""
        data_dict = {"triggerID": task.stop_trigger,
                     "instrumentID": instrumentID, "Process_status": 6}

        return data_dict

    def _check_rti_connector_publisher_flow_topic(self, es_flow_topic, xml_file):
        writers = self._parse_command_xml_command_topic(xml_file)["data_writers"]

        if f"{es_flow_topic}Pub" in [writer_key for writer_key in writers.keys()]:
            pass
        else:
            raise ddsTopicError(es_flow_topic, xml_file)

    def _check_rti_connector_subscriber_flow_topic(self, es_flow_topic, xml_file):
        readers = self._parse_command_xml_command_topic(xml_file)["data_readers"]

        if f"{es_flow_topic}Sub" in [reader_key for reader_key in readers.keys()]:
            pass
        else:
            raise ddsTopicError(es_flow_topic, xml_file)

    def _parse_flow_xml_flow_topic(self, flow_xml):

        flow_dict = self._load_xml(flow_xml)
        data_reader = {}
        data_writer = {}

        domain_participant = flow_dict["dds"]['domain_participant_library']['domain_participant']

        for element in domain_participant:
            if "subscriber" in element.keys():
                data_reader.update({element["subscriber"]['@name']: element["subscriber"]["data_reader"]})
            if "publisher" in element.keys():
                data_writer.update({element["publisher"]["@name"]: element["publisher"]["data_writer"]})

        return {"data_writers": data_writer, "data_readers": data_reader}

    def spin_studio_demo_MAGIC(self, task, sequencer_instrumentID):
        """DEMO: dont use it not part of ES, delete it later"""
        # todo !!!!! DELETE !!!!!
        self._write_dds_fct_trigger(ES_FCT, task, sequencer_instrumentID, FLOWXML)
        logging.debug(f"{datetime.datetime.now()}-> Spinstudio is running....")


class Task:
    def __str__(self):
        return f"start_trigger: {self.start_trigger}"

    def __init__(self, task_dict: dict):

        self.commands = []
        self.done_commands = []
        self.all_commands = []

        if "Command" in list(task_dict.keys()):
            if type(task_dict["Command"]) != list:
                command_dict = task_dict["Command"]
                self.commands.append(Command(command_dict))
                self.all_commands.append(Command(command_dict))
            else:
                for command_dict in task_dict["Command"]:
                    self.commands.append(Command(command_dict))
                    self.all_commands.append(Command(command_dict))

        self.start_trigger = task_dict["startTrigger"]
        self.stop_trigger = task_dict["endTrigger"]
        self.presetID = task_dict["presetID"]
        self.waitMode = task_dict["WaitMode"]
        self.waitTime = int(task_dict["WaitTime"])
        self.num_commands = len(self.commands)

    def list_of_command_transactionIds(self):

        return [command.transactionID for command in self.commands]

    def put_command_to_done(self, command_transactionID):
        """appends a certain command to the done_commands list"""
        transactionIDs = self.list_of_command_transactionIds()
        if command_transactionID in transactionIDs and len(transactionIDs) != 0:
            index = transactionIDs.index(command_transactionID)
            command = self.commands.pop(index)
            #probabbly deprecated
            #if actionKind(command.action_kind) == actionKind.direct_control:
            #    command.command_done_flag.set()

            self.done_commands.append(command)


class Command:
    
    def __init__(self, command_dict: dict):

        if command_dict:
            self.moduleID = command_dict["MSCC"]["moduleID"]
            self.transactionID = command_dict["MSCC"]["GCD"]["transactionID"]
            self.priority = commandPriority[command_dict["MSCC"]["GCD"]["Priority"]]
            self.action_kind = actionKind[command_dict["MSCC"]["GCD"]["Action_kind"]]
            self.mscd = dict(command_dict["MSCC"]["MSCD"])
            self.sessionID = command_dict["MSCC"]["GCD"]["sessionID"]

            self.mscd["Mode"] = MSCD_mode[self.mscd["Mode"]]
            self.mscd["Mode"] = self.mscd["Mode"].value

            #probably action kind is deprecated
            # if self.action_kind == actionKind.direct_control:
            #     self.command_done_flag = threading.Event()
            #     self.command_done_flag.clear()


        #this part might get deleted
        #self.params = {}

        # for key, value in command_dict.items():
        #
        #     if key not in ["moduleID", "transactionID", "Action_kind", "Priority"]:
        #         self.params.update({key: value})


class Module:

    def __init__(self, module_dict):

        self.uniqueID = module_dict["UniqueID"]
        self.moduleID = module_dict["ModuleID"]
        self.address = module_dict["Address"]

    def __str__(self):
        return f"{self.uniqueID}: {self.moduleID}"




if __name__ == "__main__":

    logging.basicConfig(level=logging.DEBUG)
    testS = Session(r"data\xmls\tasklistDummy.xml")
    inID = testS.get_instrumentID()
    command = testS.tasks[0].commands[0]
    module = testS.get_module_list()[-1]

#for demo purposses
    xml_file = r"data\xmls\spinStudioDemo.xml"

    with open(xml_file, "r") as file:
        xml_dict = parse(file.read())
    task = Task(xml_dict["Tasks"]["Task"])
    spinStudio = threading.Timer(interval=1, function=testS.spin_studio_demo_MAGIC, args=(task, inID,))
    spinStudio.start()
#
    testS.session_run()