#!/usr/bin/env python3
#coding:utf-8

import getopt, asyncio, json, sys, configparser, signal, logging, re
from pyvlx import PyVLX, Position, UnknownPosition, PyVLXException, OpeningDevice
from time import sleep
import paho.mqtt.client as mqtt

__usage__ = """
 usage: python mqtt-velux.py [options] configuration_file
 options are:
  -h or --help      display this help
  -v or --verbose   increase amount of reassuring messages
  -d or --debug     maximum verbosity published to mqtt topics
"""

class DebugStreamHandler(logging.StreamHandler):
    def __init__(self):
        logging.StreamHandler.__init__(self)
    def emit(self, record):
        if not parms["debug"]:
            logging.StreamHandler.emit(self, record)
            return
        self.format(record)
        try:
            sub.publish(
                config.get("mqtt", "response") + "/mqtt-velux/system/" + record.levelname.lower(),
                record.message,
                retain=eval(config.get("mqtt", "retain")))
        except Exception as e:
            print(e)

logger = logging.getLogger('pyvlx')
logging.basicConfig(format='%(asctime)s %(message)s',handlers=[DebugStreamHandler()])
parms = {}
config = {}
sub = {}
vlx = {}
done = 0

def on_mqtt_connect(client, userdata, flags, rc):
    logger.warning("mqtt connected with result code "+str(rc))
    client.subscribe(config.get("mqtt", "prefix") + "/#")

def on_mqtt_message(client, loop, msg):
    try:
        device = msg.topic.split(config.get("mqtt", "prefix") + "/", 1)[1]
        if device == "echo":
            sub.publish(
                config.get("mqtt", "response") + "/echo",
                msg.payload.decode('utf-8'),
                retain=eval(config.get("mqtt", "retain")))
            return
        payload = msg.payload.decode('utf-8').lower()
        logger.info("message received @%s: %s" % (msg.topic, payload))
        node = re.sub("-position$", "", device.replace("/", "-"))
        if not node in vlx.nodes:
            raise Exception("unknown node: " + node)
        asyncio.run_coroutine_threadsafe(vlx_set_position(node, payload), loop)
    except Exception as msg:
        logger.error(msg)

async def vlx_set_position(node, pos):
    if pos == "close":
        pos = "closed"
    pct = 0 if pos == "open" else 100 if pos == "closed" else eval(pos) if pos.isdigit() else None
    if pct is None:
        logger.error("invalid position for @%s: %s" % (node, pos))
        return
    logger.info("setting position @%s: %s" % (node, pct))
    await vlx.nodes[node].set_position(Position(position_percent=pct), wait_for_completion=False)

async def on_device_updated(node):
    if not isinstance(node, OpeningDevice):
        return
    if node.position == UnknownPosition():
        logger.info("device position unknown: %s" % node.name)
        return
    pct = node.position.position_percent
    msg = "open" if pct == 0 else "closed" if pct == 100 else str(pct)
    logger.info("device updated: %s = %s" % (node.name, msg))
    sub.publish(
        config.get("mqtt", "response") + "/" + node.name.replace("-", "/") + "/position",
        msg,
        retain=eval(config.get("mqtt", "retain")))

async def main(loop):
    global parms, config, logger, sub, vlx, done
    logger.setLevel(logging.ERROR)
    try:
        opts, args = getopt.getopt(sys.argv[1:], "dhv", ['debug', 'help', 'verbose'])
    except getopt.error as msg:
        print(msg)
        print(__usage__)
        return 1
    # process options
    parms["debug"] = False
    for o, a in opts:
        if o == '-h' or o == '--help':
            print(__usage__)
            return 0
        if o == '-d' or o == '--debug':
            parms["debug"] = True
            logger.setLevel(logging.DEBUG)
        elif o == '-v' or o == '--verbose':
            if logger.getEffectiveLevel() == logging.ERROR:
                logger.setLevel(logging.WARNING)
            elif logger.getEffectiveLevel() == logging.WARNING:
                logger.setLevel(logging.INFO)
            else:
                logger.setLevel(logging.DEBUG)
    # check arguments
    if len(args) < 1:
        logger.error("at least 1 argument required")
        logger.error(__usage__)
        return 2
    # read config
    config = configparser.ConfigParser()
    config.optionxform = str
    config.read(args.pop(0))

    # mqtt
    sub = mqtt.Client(userdata=loop)
    sub.on_connect = on_mqtt_connect
    sub.on_message = on_mqtt_message
    if eval(config.get("mqtt", "auth")):
        sub.username_pw_set(config.get("mqtt", "user"), config.get("mqtt", "password"))
    sub.connect(config.get("mqtt", "hostname"), eval(config.get("mqtt", "port")), 60)
    sub.loop_start()

    # velux
    if not done:
        vlx = PyVLX(host=config.get("velux", "hostname"), password=config.get("velux", "password"), loop=loop)
        nodes = []
        await vlx.load_nodes()
        for n in vlx.nodes:
            n.register_device_updated_cb(on_device_updated)
            logger.info(str(n))
            nodes.append(n.name)
        nodes = ", ".join(nodes)
        logger.info("started: " + nodes)
        sub.publish(
            config.get("mqtt", "response") + "/mqtt-velux/system/message",
            "started: " + nodes,
            retain=eval(config.get("mqtt", "retain")))

    logger.info("looping...")
    while not done:
        await asyncio.sleep(0.1)

    logger.info("done.")
    sub.loop_stop()
    await vlx.disconnect()

    sub.publish(
        config.get("mqtt", "response") + "/mqtt-velux/system/message",
        "ended.",
        retain=eval(config.get("mqtt", "retain")))
    logger.info("ended.")

def signal_handler(signum, frame):
    global done
    signame = signal.Signals(signum).name
    logger.error("%s received" % signame)
    done = 1

if __name__ == '__main__':
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGHUP, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    try:
        LOOP = asyncio.get_event_loop()
        LOOP.run_until_complete(main(LOOP))
    except Exception as msg:
        logger.error(msg)
        raise
    LOOP.close()
