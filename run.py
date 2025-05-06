from pywitch import PyWitchTMI, run_forever
from pywitch import PyWitchStreamInfo
from twitchAPI.twitch import Twitch
from twitchAPI.oauth import UserAuthenticator
from twitchAPI.types import AuthScope
from twitchAPI.types import TwitchAPIException
import logging
import atexit
import time
import csv
import requests
from datetime import datetime
import os.path
import os
from dotenv import load_dotenv, dotenv_values

load_dotenv()

# settings
clip_threshold = 2.2                # percent of avg chat activity needed to trigger clip, 1.0 is 100% (exactly the average).
chat_count_trap_length = 1000       # how many tmi calls back do we store to calculate the average chat speed. default 1000, using lower for fast testing
chat_count_trap_time = 20           # how many tmi calls back in time do we calculate increase from.
chat_increase_list_length = 100     # how many past chat count increases to store for calculating average increase.
lockout_timer = 20                  # after creating a clip, how long before another clip can be created on that channel. some value above 0 is needed to not create multiple clips from same chat event.
total_tick_length = 5               # how long in seconds should we take to perform one chat data pull from each target channel - give a total amount in seconds which is then divided by the amount of online channels (value of 10s with 50 online channels = wait 0.2s between each call). needs to be above 0 or tmi data packets will be too small to calculate increases.
min_chat_increase = 20              # the minimum amount of chat messages more than the average we need to trigger a clip. - this is here to account for tracking low view count streams. where an increase of 2 avg messages per tick may jump to 7/8 messages per tick through natural conversation or chatbot messages. this is a large increase compared to the average but not nessecarily a clippable moment.
clip_delay = 5                      # how long in seconds to wait to execute clip from moment of detection. (to allow the clip to include things after the chat spike)

settings_track_offline_channels = True
settings_log_chat_channels = True
settings_log_chat_main = True
settings_logging_mode = 0 # 0 = Full Logging, 1 = Standard Logging, TODO: (do we need 3 = Lightweight logging too?)
# Do we need this setting? we are already using above log settings to turn individual elements on/off.
# Could set this up as a "profile" system to store settings for indidivudal log settings, or just ignore and allow user
# to edit individual log settings

# variable setup
app_id = os.getenv("APP_ID")
app_secret = os.getenv("APP_SECRET")
user_token = os.getenv("USER_TOKEN")
users = {} # shared user list minimizes the number of requests
target_channels = [] # this gets set in load_channels() - is only here in case we use before that happens. could be removed later.
twitch = Twitch(app_id, app_secret)

# setup TwitchAPI "User" Authentication - we need this authentication level for: clip creation.
target_scope = [AuthScope.CLIPS_EDIT]
auth = UserAuthenticator(twitch, target_scope, force_verify=False)
token, refresh_token = auth.authenticate() # this will open your default browser and prompt you with the twitch verification website
twitch.set_user_authentication(token, target_scope, refresh_token)

# clean up
def cleanup_chatloop():
    # close clips csv
    clips_csv.close()
atexit.register(cleanup_chatloop)

# open clips csv
clips_csv = open('clips.csv', 'a', encoding='UTF8', newline="")
clips_write = csv.writer(clips_csv)
if not os.path.getsize('clips.csv'):
    clips_csv_headers = [
        "Channel Name",
        "Category",
        "URL",
        "Increase",
        "Avg",
        "Difference",
        "Time"
    ]
    clips_write.writerow(clips_csv_headers)

# setup loggers - channel specific loggers setup inside channel class.
formatter = logging.Formatter('%(asctime)s - %(message)s')
def setup_logger(name, log_file, level=logging.INFO, console=False):
    """To setup as many loggers as you want"""
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.addHandler(file_handler)
    if console == True:
        logger.addHandler(console_handler)
    return logger

chat_logger = setup_logger('chatlog', 'chat.log')
clip_logger = setup_logger('clipslog', 'clips.log')
event_logger = setup_logger('eventslog', 'events.log',console=True)


class Channel:

    def __init__(self, _channel_name, _category):
        self.channel_name = _channel_name
        self.category = _category
        self.update_stream_info()
        self.initialize_tracking()
        self.channel_chat_logger = setup_logger(self.channel_name + '_chatlog', 'channel_logs/' + self.channel_name + '_chat.log')
        self.previous_offline = False

    def initialize_tracking(self):
        self.chat_count = 1  # we start at 1 to avoid 'divide by zero' problems on chat_count_past
        self.chat_count_past = 1
        self.chat_count_trap = []
        self.chat_count_increase = 0
        self.chat_count_increase_frac = 0
        self.chat_count_difference = 0
        self.chat_increase_list = []
        self.chat_increase_avg = 0
        self.lockout = 0
        event_logger.info("[" + self.channel_name + "] - Channel Tracking Initialized")

    def update_stream_info(self):
        self.channel_info = twitch.get_users(logins=[self.channel_name])
        self.id = self.channel_info['data'][0]['id']
        self.broadcast_info = twitch.get_channel_information(broadcaster_id=[self.id])
        self.stream_info = twitch.get_streams(user_id=[self.id])
        event_logger.info("[" + self.channel_name + "] - Updated Stream Info")

    def channel_is_offline(self):
        self.update_stream_info()
        if not self.stream_info['data']:
            return True
        else:
            return False

    def channel_went_offline(self):
        self.previous_offline = True
        self.initialize_tracking()
        event_logger.info("[" + str(self.channel_name) + "] Channel Went OFFLINE- Initializing Channel Tracking.")
        self.channel_chat_logger.info(" [ CHANNEL WENT OFFLINE ]")

    def channel_went_online(self):
        self.previous_offline = False
        self.initialize_tracking()
        event_logger.info("[" + str(self.channel_name) + "] Channel Came ONLINE- Initializing Channel Tracking.")
        self.channel_chat_logger.info(" [ CHANNEL CAME ONLINE ]")

    '''
    # chat info callback - get info from stream (same as running GET https://api.twitch.tv/helix/users?login=<login name>&id=<user ID> api call)
    def info_callback(self, data):
        print("\n -- setting channel info for " + self.channel_name + ": ")
        print(data)
        self.id = data["user_id"]

    # setup channel info - returns channel info dictionary (data) via callback above.
        # IS THIS STILL NESSESARY? We are using twitch.get_users to retrive channel info as needed.
        # May be needed to check if a channel is still online or offline - could we again just check online/offline status with a twitch.___ api call????
    def setup_info(self):
        global user_token
        global users
        target_info = PyWitchStreamInfo(
            channel = self.channel_name,
            token = user_token,
            callback = self.info_callback,
            users = users,
            interval = 1,
            verbose = True
        )
        target_info.start()
    '''

    # tmi callback - runs everytime messages are sent to the twitch chat
    def tmi_callback(self, data):
        # data looks like: ['display_name', 'event_time', 'user_id', 'login', 'message', 'event_raw']
        print("    " + str(data))
        self.chat_count += 1
        if settings_log_chat_main:
            chat_logger.info( "chat_count:" + str(self.chat_count) + " [" + self.channel_name + "] [" + data['display_name'] + "] " + data['message'] )
        if settings_log_chat_channels:
            self.channel_chat_logger.info( "chat_count:" + str(self.chat_count) + " [" + self.channel_name + "] [" + data['display_name'] + "] " + data['message'])

    # setup tmi (twitch messaging interface) - returns chat messages with their data via callback above.
    def setup_tmi(self):
        global user_token
        global users
        tmi = PyWitchTMI(
            channel=self.channel_name,
            token=user_token,
            callback=self.tmi_callback,  # Optional
            users=users,  # Optional, but strongly recomended
            verbose=True,  # Optional
        )
        tmi.start()
        # tmi.send(' OOOO ') # send message in chat example

    # make clip
    def get_clip(self):

        time.sleep(clip_delay)

        clip_trigger_info = \
            " ~ (inc:" + str(self.chat_count_increase) \
            + ", avg:" + str(round(self.chat_increase_avg, 2)) \
            + ", diff:" + str(round(self.chat_count_increase / self.chat_increase_avg, 2)) \
            + ")"

        # dont make clip (return) if channel is offline
        if self.channel_is_offline():
            error_clip_offline = self.channel_name + " | [CLIP CREATE FAILED]: Channel Offline" + clip_trigger_info
            clip_logger.info(error_clip_offline)
            print( "Error: " + str(error_clip_offline) )
            return

        global twitch

        # print some stuff so we see the clip happen when watching terminal
        print("CREATING CLIP!!!!!!!!!!!!!!!!!")
        print("CLIPPPPPPPP")
        print("CLIPPPPPPPP")

        # create clip
        try:
            clip = twitch.create_clip(self.id, False)
            if 'error' in clip.keys():
                raise TwitchAPIException(clip)
        except TwitchAPIException as error:
            # if we get a category specific clipping error, include the category in the error log
            if 'Clipping is restricted for this category on this channel.' in clip.keys():
                self.update_stream_info()
                error = "[Category: " + self.stream_info['data'][0]['game_name'] + "]" + error
            clip_logger.info( self.channel_name + " | [CLIP CREATE FAILED]: " + str(error) + clip_trigger_info )
            print( "Error: " + str(error) )
            return
        else: # print feedback to terminal
            print(clip)
            print(clip['data'][0]['edit_url'])

        # write to log
        clip_logger.info( self.channel_name + " | " + clip['data'][0]['edit_url'] + clip_trigger_info )

        # write to csv
        clip_row = [
            self.channel_name,
            self.category,
            clip["data"][0]["edit_url"],
            str(self.chat_count_increase),
            str(round(self.chat_increase_avg, 2)),
            str(round(self.chat_count_increase / self.chat_increase_avg, 2)),
            datetime.now()
        ]
        clips_write.writerow(clip_row)

def load_channels():

    global target_channels
    target_channels = [] # re-initialize
    category = "DEFAULT"

    with open('target_channels.txt', 'r') as file_object:
        file_contents = file_object.readlines()
        for line in file_contents:

            line = line.strip()

            # store category else store channel name
            if line.startswith("//"):
                category = line[2:]
                print("\n CATEGORY CHANGE: " + category)
                continue

            # create channel object
            if line and not line.startswith("#"): # use "#" for commenting out in text file
                channel_name = line
                channel_info = twitch.get_users(logins=[channel_name])
                print( "[" + str(channel_name) + "] Channel Data Recieved: " + str(channel_info) )
                if channel_info['data']:
                    target_channels.append(Channel(channel_name, category))
                else:
                    channel_error = "Error adding channel [" + channel_name + "], no channel id data recieved, is the channel banned or the name typed incorrectly?"
                    event_logger.info(channel_error)

def add_channel(*args):
    for c in range(len(args)):
        channel_info = twitch.get_users(logins=[c])
        if channel_info['data']: # check if channel returns a data array for channel info
            target_channels.append(Channel(args[c]))
        with open('target_channels.txt', 'a+') as file_object:
            file_object.seek(0) # go to start of file
            data = file_object.read(100)
            if len(data) > 0: # if file is empty write new line
                file_object.write("\n")
            file_object.write(args[c])

# run program
def run_clipper():

    load_channels()
    print(target_channels)

    # setup tmi for chat loops
    for i in range(len(target_channels)):
        t = target_channels[i]
        t.setup_tmi()

    # chat count loop
    while True:

        event_logger.info("CALCULATING TICK LENGTH")

        # calculate how many online channels and set tick length
        online_channel_count = len(target_channels)
        for i in range(len(target_channels)):
            if target_channels[i].channel_is_offline():
                online_channel_count -= 1
        event_logger.info("total_tick_length: " + str(total_tick_length) + "   online_channel_count: " + str(online_channel_count))
        tick_length = round(total_tick_length / online_channel_count, 2)
        event_logger.info( "    TICK LENGTH: " + str(tick_length) )

        for i in range(len(target_channels)):

            t = target_channels[i]

            # dont process offline channels, manage channels coming online or offline.
            if settings_track_offline_channels:
                if t.channel_is_offline():
                    print( " [" + t.channel_name + "] - Channel Offline" )
                    if t.previous_offline == False:
                        t.channel_went_offline()
                    t.previous_offline = True
                    continue
                else:
                    if t.previous_offline == True:
                        t.channel_went_online()
                    t.previous_offline = False


            try:

                # store current chat count into trap list at position 0
                t.chat_count_trap.insert(0,t.chat_count)

                # destroy last entry in trap list if full
                if len(t.chat_count_trap) >= chat_count_trap_length:
                    t.chat_count_trap.pop()

                # set past chat value if trap_time has been exceeded
                if len(t.chat_count_trap) > chat_count_trap_time:
                    t.chat_count_past = t.chat_count_trap[chat_count_trap_time-1]
                else: t.chat_count_past = 1

                # set count increase since past count and turn into percentage/decimal
                t.chat_count_increase = t.chat_count - t.chat_count_past
                if t.chat_count_increase > 0:
                    t.chat_count_increase_frac = t.chat_count_increase / t.chat_count_past
                else: t.chat_count_increase_frac = 0

                # add count increase to increase list (used for calculating average), remove if above max length
                if t.chat_count_increase > 0:
                    t.chat_increase_list.insert(0, t.chat_count_increase)
                if len(t.chat_increase_list) >= chat_increase_list_length:
                    t.chat_increase_list.pop()

                # calculate average increase, represent as fraction, then print feedback
                if len(t.chat_increase_list) > 0:
                    t.chat_increase_avg = sum(t.chat_increase_list) / len(t.chat_increase_list)
                if t.chat_count_increase > 0 and t.chat_increase_avg > 0: # to avoid divide by zero error
                    t.chat_count_difference = round(t.chat_count_increase / t.chat_increase_avg, 2)
                else: t.chat_count_difference = 0
                tick_data = "  [" + t.channel_name + "]" \
                       + " current:" + str(t.chat_count) \
                       + " past:" + str(t.chat_count_past) \
                       + " increase:" + str(t.chat_count_increase) \
                       + " inc_frac:" + str(round(t.chat_count_increase_frac,2)) \
                       + " avg_inc:" + str(round(t.chat_increase_avg,2)) \
                       + " diff:" + str(t.chat_count_difference) \
                       + " lockout:" + str(t.lockout) \
                       + " trap_len:" + str(len(t.chat_count_trap))
                t.channel_chat_logger.info(tick_data)
                print("\n " + tick_data + "\n")

                # if increase is x bigger than avg increase then trigger clip
                if t.chat_count_increase > (clip_threshold * t.chat_increase_avg) \
                and t.chat_count_increase >= min_chat_increase \
                and len(t.chat_count_trap) > (chat_count_trap_length*0.1) \
                and t.lockout == 0:
                    t.lockout = lockout_timer
                    t.get_clip()

                # move lockout timer
                if t.lockout > 0:
                    t.lockout -= 1

                # wait some time to gather enough data, will want to change this as number of target channels goes up.
                time.sleep(tick_length)

            except (KeyboardInterrupt, SystemExit) as e:
                cleanup_chatloop()

    # run forever (for pywitch tmi) - not nessecary when checking tmi in loop, needed for continually running the tmi outside of a loop though.
    #run_forever()

run_clipper()
