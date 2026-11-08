"""PLAF203 MQTT wire constants, enums, and message codecs."""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime
import enum
import hashlib
import os
import uuid
from pathlib import Path
from typing import Mapping, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

class Commands:
    ATTR_GET_SERVICE = "ATTR_GET_SERVICE"
    ATTR_PUSH_EVENT = "ATTR_PUSH_EVENT"
    ATTR_SET_SERVICE = "ATTR_SET_SERVICE"
    DEVICE_CONFIG_SYNC = "DEVICE_CONFIG_SYNC"
    DEVICE_LOG_REPORT_EVENT = "DEVICE_LOG_REPORT_EVENT"
    DEVICE_REBOOT = "DEVICE_REBOOT"
    DEVICE_START_EVENT = "DEVICE_START_EVENT"
    ERROR_EVENT = "ERROR_EVENT"
    FEEDING_PLAN_SERVICE = "FEEDING_PLAN_SERVICE"
    GET_CONFIG = "GET_CONFIG"
    GET_FEEDING_PLAN_EVENT = "GET_FEEDING_PLAN_EVENT"
    GRAIN_OUTPUT_EVENT = "GRAIN_OUTPUT_EVENT"
    HEARTBEAT = "HEARTBEAT"
    INITIALIZE_SD_CARD_SERVICE = "INITIALIZE_SD_CARD_SERVICE"
    MANUAL_FEEDING_SERVICE = "MANUAL_FEEDING_SERVICE"
    NTP = "NTP"
    NTP_SYNC = "NTP_SYNC"
    RESTORE = "RESTORE"
    WIFI_RECONNECT_SERVICE = "WIFI_RECONNECT_SERVICE"


class MessageTopics:
    DEVICE_PRODUCT_ID = "PLAF203"

    def __init__(self, device_serial_number: str):
        self.device_serial_number = device_serial_number

    def sub(self, channel: str) -> str:
        return f"dl/{self.DEVICE_PRODUCT_ID}/{self.device_serial_number}/device/{channel}/sub"

    def post(self, channel: str) -> str:
        return f"dl/{self.DEVICE_PRODUCT_ID}/{self.device_serial_number}/device/{channel}/post"

@dataclass
class MessageId:
    data: str

    @staticmethod
    def generate() -> MessageId:
        uuid_random = uuid.uuid4()
        hash_object = hashlib.sha256(str(uuid_random).encode())

        return MessageId(hash_object.hexdigest()[:32])

# Remark: Timestamps on the device are in ms since epoch
@dataclass
class Timestamp:
    value: datetime.datetime

    @staticmethod
    def now() -> Timestamp:
        return Timestamp(datetime.datetime.now(_local_timezone()))

    @staticmethod
    def from_timestamp_epoch_ms(timestamp_epoch_ms: int) -> Timestamp:
        # Always assume same time zone as backend as that information is not
        # delivered with each message. The backend needs to detect if this
        # is incorrect and adjust the time on the device accordingly
        return Timestamp(datetime.datetime.fromtimestamp(timestamp_epoch_ms / 1000, _local_timezone()))

    def to_timestamp_epoch_ms(self) -> int:
        return int(self.value.timestamp() * 1000)

    def to_timezone_offset_hours(self) -> int | float:
        hours = self.value.utcoffset().total_seconds() / 3600
        return int(hours) if hours.is_integer() else hours

    def abs_delta(self, other: Timestamp) -> datetime.timedelta:
        return abs(self.value - other.value)


def _local_timezone() -> datetime.tzinfo:
    timezone_names = [os.environ.get('TZ')]
    try:
        timezone_names.append(Path('/etc/timezone').read_text().strip())
    except OSError:
        pass

    localtime = Path('/etc/localtime')
    try:
        target = localtime.resolve()
        marker = '/zoneinfo/'
        if marker in str(target):
            timezone_names.append(str(target).split(marker, 1)[1])
    except OSError:
        pass

    for timezone_name in timezone_names:
        if not timezone_name:
            continue
        try:
            return ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            continue

    return datetime.datetime.now().astimezone().tzinfo


def _timezone_payload(timestamp: Timestamp) -> dict:
    instant_utc = timestamp.value.astimezone(datetime.timezone.utc)
    timezone = timestamp.value.tzinfo
    current_offset_seconds = int(instant_utc.astimezone(timezone).utcoffset().total_seconds())
    transitions = []
    search_start = instant_utc

    for _ in range(2):
        transition = _next_timezone_transition(search_start, timezone)
        if transition is None:
            # Fixed-offset zones still send the required schema. A zero
            # transition timestamp means there is no upcoming offset change.
            transitions.append((current_offset_seconds, 0))
            continue
        transition_utc, next_offset_seconds = transition
        transitions.append((next_offset_seconds, int(transition_utc.timestamp() * 1000)))
        search_start = transition_utc + datetime.timedelta(seconds=1)
        current_offset_seconds = next_offset_seconds

    return {
        'timezone': timestamp.to_timezone_offset_hours(),
        'timezoneOffsetSeconds': int(timestamp.value.utcoffset().total_seconds()),
        'nextDSTOffsetSeconds': transitions[0][0],
        'nextDSTTransitionTs': transitions[0][1],
        'secondNextDSTOffsetSeconds': transitions[1][0],
        'secondNextDSTTransitionTs': transitions[1][1],
    }


def _next_timezone_transition(start_utc: datetime.datetime, timezone: datetime.tzinfo):
    start_utc = start_utc.astimezone(datetime.timezone.utc)
    current_offset = start_utc.astimezone(timezone).utcoffset()
    cursor = start_utc

    # Three years covers two DST transitions while still tolerating zones that
    # suspend seasonal changes for a year.
    for _ in range(366 * 3):
        candidate = cursor + datetime.timedelta(days=1)
        if candidate.astimezone(timezone).utcoffset() != current_offset:
            low = int(cursor.timestamp())
            high = int(candidate.timestamp())
            while low + 1 < high:
                middle = (low + high) // 2
                middle_time = datetime.datetime.fromtimestamp(middle, datetime.timezone.utc)
                if middle_time.astimezone(timezone).utcoffset() == current_offset:
                    low = middle
                else:
                    high = middle
            transition = datetime.datetime.fromtimestamp(high, datetime.timezone.utc)
            offset_seconds = int(transition.astimezone(timezone).utcoffset().total_seconds())
            return transition, offset_seconds
        cursor = candidate

    return None

class Code(enum.Enum):
    OK = 0
    ERROR_1 = 1
    ERROR_2 = 2
    ERROR_3 = 3
    ERROR_4 = 4

    # Triggers a wifi reset on the device
    # Can be set as code on ATTR_PUSH_EVENT and NTP
    ERROR_DEVICE_NOT_BOUND = 2030

    def is_ok(self):
        return self == Code.OK

    def is_error(self):
        return not self == Code.OK

class AgingType(enum.Enum):
    INVALID = 0
    NON_SCHEDULED_ENABLED = 1
    SCHEDULED_ENABLED = 2

class NightVision(enum.Enum):
    AUTOMATIC = 0
    OPEN = 1
    CLOSE = 2

class Resolution(enum.Enum):
    P720 = 0
    P1080 = 1

class BowlMode(enum.Enum):
    SINGLE_BOWL = 'SINGLE_BOWL'
    DOUBLE_BOWL = 'DOUBLE_BOWL'

    @staticmethod
    def from_mqtt_payload_value(value: str) -> BowlMode:
        # Some Petlibro surfaces describe this configuration as "dual bowl",
        # while the protocol naming pairs SINGLE_BOWL with DOUBLE_BOWL.
        # Accept DUAL_BOWL if a firmware variant reports it, but always send
        # the canonical DOUBLE_BOWL value used by this integration.
        if value == 'DUAL_BOWL':
            return BowlMode.DOUBLE_BOWL
        return BowlMode(value)

class VideoRecordMode(enum.Enum):
    CONTINUOUS = 0
    MOTION_DETECTION = 1

class MotionDetectionSensitivity(enum.Enum):
    LOW = 0
    MEDIUM = 1
    HIGH = 2

class MotionDetectionRange(enum.Enum):
    SMALL = 0
    MEDIUM = 1
    LARGE = 2

class SoundDetectionSensitivity(enum.Enum):
    LOW = 0
    MEDIUM = 1
    HIGH = 2

class PowerMode(enum.Enum):
    USB = 1
    BATTERY = 2

class PowerType(enum.Enum):
    INVALID = 0
    USB_ONLY = 1
    BATTERY_ONLY = 2
    USB_AND_BATTERY = 3

class SdCardState(enum.Enum):
    NOT_AVAILABLE = 0
    AVAILABLE = 1
    INITIALIZING = 2

class SdCardFileSystem(enum.Enum):
    INVALID = 0
    FAT32 = 1
    FAT = 2
    EXFAT = 3
    NTFS = 4
    UNKNOWN = 5

    @staticmethod
    def from_mqtt_payload_value(value: str) -> SdCardFileSystem:
        if value == 'FAT32':
            return SdCardFileSystem.FAT32
        elif value == 'FAT':
            return SdCardFileSystem.FAT
        elif value == 'EXFAT':
            return SdCardFileSystem.EXFAT
        elif value == 'NTFS':
            return SdCardFileSystem.NTFS
        elif value in ('unknown type', 'unkown type'):
            return SdCardFileSystem.UNKNOWN
        else:
            return SdCardFileSystem.INVALID

class WifiType(enum.Enum):
    TYPE_0 = 0
    TYPE_1 = 1
    TYPE_2 = 2

class ExecStep(enum.Enum):
    INVALID = 0
    GRAIN_START = 1
    GRAIN_END = 2
    GRAIN_BLOCKING = 3

    @staticmethod
    def from_mqtt_payload_value(value: str) -> ExecStep:
        if value == 'GRAIN_START':
            return ExecStep.GRAIN_START
        elif value == 'GRAIN_END':
            return ExecStep.GRAIN_END
        elif value == 'GRAIN_BLOCKING':
            return ExecStep.GRAIN_BLOCKING
        else:
            return ExecStep.INVALID

class GrainOutputType(enum.Enum):
    INVALID = 0
    FEED_PLAN = 1
    MANUAL_FEED = 2
    MANUAL_FEED_BUTTON = 3

@dataclass
class PercentageInt:
    value: int

    def __init__(self, percentage_value: int):
        if percentage_value < 0 or percentage_value > 100:
            raise ValueError("Incorrect range for percentage value: {}".format(percentage_value))

        self.value = percentage_value

    def value_get(self) -> int:
        return self.value

# The remote device considers all food plan HH:MM timestamps zoned to UTC
# Since the user should be able to configure these in their local timezone,
# this deals with converting the timestamps between UTC and the local timezone
@dataclass
class HourMinTimestamp:
    time: datetime.time

    @staticmethod
    def create_from_local_timezone(hour: int, minute: int) -> HourMinTimestamp:
        # Get system configured timezome
        now = datetime.datetime.now(_local_timezone())

        return HourMinTimestamp(datetime.time(hour = hour, minute = minute, tzinfo = now.tzinfo))

    @staticmethod
    def create_from_utc(hour: int, minute: int):
        utc_time = datetime.time(hour = hour, minute = minute, tzinfo = datetime.timezone.utc)

        utc_remote_datetime = datetime.datetime.combine(datetime.date.today(), utc_time)

        # Get system configured timezome
        now = datetime.datetime.now(_local_timezone())

        return HourMinTimestamp(utc_remote_datetime.astimezone(now.tzinfo).time())

    @staticmethod
    def from_dict(data: dict) -> HourMinTimestamp:
        return HourMinTimestamp.create_from_local_timezone(
            hour = int(data['hour']),
            minute = int(data['minute']))

    def to_dict(self) -> dict:
        return {
            'hour': self.time.hour,
            'minute': self.time.minute,
        }

    def to_mqtt_payload_value(self) -> str:
        utc_time = self._time_to_utc_timezone()

        return "{:02}:{:02}".format(utc_time.hour, utc_time.minute)

    @staticmethod
    def from_mqtt_payload_value(value: str) -> HourMinTimestamp:
        hour, minute = map(int, value.split(':'))

        return HourMinTimestamp.create_from_utc(hour, minute)

    def _time_to_utc_timezone(self) -> datetime.time:
        local_datetime = datetime.datetime.combine(datetime.date.today(), self.time)

        return local_datetime.astimezone(datetime.timezone.utc).time()


class Weekday(enum.Enum):
    INVALID = 0
    MONDAY = 1
    TUESDAY = 2
    WEDNESDAY = 3
    THURSDAY = 4
    FRIDAY = 5
    SATURDAY = 6
    SUNDAY = 7

@dataclass
class WeekdaySchedule:
    value: set[Weekday]

    @staticmethod
    def create(*args: Weekday) -> WeekdaySchedule:
        return WeekdaySchedule(set(args))

    @staticmethod
    def from_list(data: list[str]) -> WeekdaySchedule:
        weekdays: list[Weekday] = []

        for item in data:
            weekdays.append(Weekday[item])

        return WeekdaySchedule(weekdays)

    def to_list(self) -> list[str]:
        return [
            item.name for item in sorted(self.value, key=lambda item: item.value)
        ]

    def to_mqtt_payload_value(self) -> list[int]:
        data = sorted(v.value for v in self.value)

        # Pad the array with 0s up to the full length of 7 elements
        data.extend([0] * (7 - len(data)))

        return data

    @staticmethod
    def from_mqtt_payload_value(value: list[int]) -> WeekdaySchedule:
        weekday_schedule = WeekdaySchedule()

        for v in value:
            weekday_schedule.set(Weekday(v))

        return weekday_schedule

# No HeartbeatOut message
@dataclass
class HeartbeatIn:
    # No msgId on this message
    timestamp: Timestamp
    count: int
    rssi: int
    wifi_type: WifiType

    @staticmethod
    def from_mqtt_payload(payload: dict) -> HeartbeatIn:
        return HeartbeatIn(
            timestamp = Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
            count = int(payload['count']),
            rssi = int(payload['rssi']),
            wifi_type = WifiType(int(payload['wifiType'])))

@dataclass
class NtpIn:
    # No msgId on this message

    # The timestamp is the current time on the device and might need re-calibration
    # So it is considered part of the actual "payload" in this case and not just
    # metadata
    # As there is no timezone provided, we assume it is the current timezone
    # of the backend. If that's not correct, we just start a calibration
    # process to fix that
    timestamp: Timestamp

    @staticmethod
    def from_mqtt_payload(payload: dict) -> NtpIn:
        return NtpIn(
            timestamp = Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
        )

@dataclass
class NtpOut:
    code: Code
    timestamp: Timestamp
    calibration_tag: bool

    def to_mqtt_payload(self) -> dict:
        return {
            # Payload does not have a message id
            'cmd': Commands.NTP,
            # ts + timezone are used to set time and zone if calibration tag is true
            'ts': self.timestamp.to_timestamp_epoch_ms(),
            'code': self.code.value,
            'calibrationTag': self.calibration_tag,
        } | _timezone_payload(self.timestamp)

@dataclass
class NtpSyncIn:
    message_id: MessageId
    timestamp: Timestamp
    code: Code

    @staticmethod
    def from_mqtt_payload(payload: dict) -> NtpSyncIn:
        return NtpSyncIn(
            message_id = MessageId(payload['msgId']),
            timestamp = Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
            code = Code(int(payload['code'])),
        )

@dataclass
class NtpSyncOut:
    message_id: MessageId
    timestamp: Timestamp

    @staticmethod
    def create() -> NtpSyncOut:
        return NtpSyncOut(
            message_id = MessageId.generate(),
            timestamp = Timestamp.now(),
        )

    def to_mqtt_payload(self) -> dict:
        return {
            'cmd': Commands.NTP_SYNC,
            'msgId': self.message_id.data,
            # ts + timezone are used to set time and zone
            'ts': self.timestamp.to_timestamp_epoch_ms(),
        } | _timezone_payload(self.timestamp)

@dataclass
class DeviceStartEventIn:
    message_id: MessageId
    timestamp: Timestamp
    success: bool
    pid: str
    uuid: str
    mac: str
    wpa3: int
    hardware_version: str
    software_version: str
    tutk_p2p_region: Optional[str] = None
    restart_reason: Optional[str] = None

    @staticmethod
    def from_mqtt_payload(payload: dict) -> DeviceStartEventIn:
        return DeviceStartEventIn(
            message_id = MessageId(payload['msgId']),
            timestamp = Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
            success = payload['success'],
            pid = payload['pid'],
            uuid = payload['uuid'],
            mac = payload['mac'],
            wpa3 = int(payload['wpa3']),
            hardware_version = payload['hardwareVersion'],
            software_version = payload['softwareVersion'],
            tutk_p2p_region = payload.get('tutkP2pRegion'),
            restart_reason = payload.get('restartReason'),
        )

@dataclass
class DeviceStartEventOut:
    message_id: MessageId
    timestamp: Timestamp
    code: Code

    @staticmethod
    def create(**kwargs) -> DeviceStartEventOut:
        return DeviceStartEventOut(
            timestamp = Timestamp.now(),
            **kwargs
        )

    def to_mqtt_payload(self) -> dict:
        return {
            'cmd': Commands.DEVICE_START_EVENT,
            'msgId': self.message_id.data,
            'ts': self.timestamp.to_timestamp_epoch_ms(),
            'code': self.code.value,
        }

@dataclass
class MqttAddr:
    host: str
    port: int

@dataclass
class DeviceConfigSyncOut:
    message_id: MessageId
    timestamp: Timestamp
    mqtt_addr: list[MqttAddr]
    https_addr: Optional[str]
    tutk_p2p_region: str

    @staticmethod
    def create(**kwargs) -> DeviceConfigSyncOut:
        return DeviceConfigSyncOut(
            message_id = MessageId.generate(),
            timestamp = Timestamp.now(),
            **kwargs
        )

    def to_mqtt_payload(self) -> dict:
        mqtt_addrs = []

        for addr in self.mqtt_addr:
            mqtt_addrs.append({
                'host': addr.host,
                'port': addr.port
            })

        payload = {
            'cmd': Commands.DEVICE_CONFIG_SYNC,
            'msgId': self.message_id.data,
            'ts': self.timestamp.to_timestamp_epoch_ms(),
            'mqttAddr': mqtt_addrs,
            'tutkP2pRegion': self.tutk_p2p_region,
        }
        if self.https_addr:
            payload['httpsAddr'] = self.https_addr
        return payload


@dataclass
class DeviceConfigSyncIn:
    message_id: MessageId
    timestamp: Timestamp
    code: Code

    @staticmethod
    def from_mqtt_payload(payload: dict) -> DeviceConfigSyncIn:
        return DeviceConfigSyncIn(
            message_id = MessageId(payload['msgId']),
            timestamp = Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
            code = Code(int(payload['code'])),
        )

@dataclass
class ManualFeedingServiceIn:
    message_id: MessageId
    timestamp: Timestamp
    # Firmware returns OEM-specific service codes (for example 2002). Keep the
    # wire integer instead of forcing it through the deliberately small Code
    # enum used by commands whose response set is closed.
    code: int

    @staticmethod
    def from_mqtt_payload(payload: dict) -> ManualFeedingServiceIn:
        return ManualFeedingServiceIn(
            message_id = MessageId(payload['msgId']),
            timestamp = Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
            code = int(payload['code']),
        )

@dataclass
class ManualFeedingServiceOut:
    message_id: MessageId
    timestamp: Timestamp
    grain_num: int

    @staticmethod
    def create(**kwargs) -> ManualFeedingServiceOut:
        return ManualFeedingServiceOut(
            message_id = MessageId.generate(),
            timestamp = Timestamp.now(),
            **kwargs
        )

    def to_mqtt_payload(self) -> dict:
        return {
            'cmd': Commands.MANUAL_FEEDING_SERVICE,
            'msgId': self.message_id.data,
            'ts': self.timestamp.to_timestamp_epoch_ms(),
            'grainNum': self.grain_num,
        }

@dataclass
class GrainOutputEventIn:
    message_id: MessageId
    timestamp: Timestamp
    finished: bool
    type_: GrainOutputType
    actual_grain_num: int
    expected_grain_num: int
    exec_time: Timestamp
    exec_step: ExecStep
    plan_id: Optional[int] = None
    retried: Optional[int] = None
    error_code: Optional[int] = None

    @staticmethod
    def from_mqtt_payload(payload: dict) -> GrainOutputEventIn:
        if not isinstance(payload['msgId'], str) or not payload['msgId']:
            raise ValueError('msgId must be a nonempty string')
        if type(payload['finished']) is not bool:
            raise ValueError('finished must be a boolean')
        for field in ('actualGrainNum', 'expectGrainNum'):
            if type(payload[field]) is not int or not 0 <= payload[field] <= 255:
                raise ValueError(f'{field} must be a portion-count integer')
        for field in ('execTime', 'ts', 'errorCode'):
            if field in payload and (
                type(payload[field]) is not int or payload[field] < 0
            ):
                raise ValueError(f'{field} must be a nonnegative integer')
        for field in ('type', 'planId', 'retried'):
            if field in payload and type(payload[field]) is not int:
                raise ValueError(f'{field} must be an integer')
        data = {
            'message_id': MessageId(payload['msgId']),
            'timestamp': Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
            'finished': payload['finished'],
            'type_': GrainOutputType(int(payload['type'])),
            'actual_grain_num': int(payload['actualGrainNum']),
            'expected_grain_num': int(payload['expectGrainNum']),
            'exec_time': Timestamp.from_timestamp_epoch_ms(int(payload['execTime'])),
            'exec_step': ExecStep[payload['execStep']],
        }

        if 'planId' in payload:
            data = data | { 'plan_id': int(payload['planId']) }

        if 'retried' in payload:
            data = data | { 'retried': int(payload['retried']) }

        if 'errorCode' in payload:
            data = data | { 'error_code': int(payload['errorCode']) }

        return GrainOutputEventIn(**data)

@dataclass
class ErrorEventIn:
    message_id: MessageId
    timestamp: Timestamp
    error_code: int
    trigger_time: Timestamp
    extend: Optional[str] = None

    @staticmethod
    def from_mqtt_payload(payload: dict) -> ErrorEventIn:
        message_id = payload['msgId']
        if not isinstance(message_id, str) or not message_id:
            raise ValueError('msgId must be a nonempty string')
        extend = payload.get('extend')
        if extend is not None:
            if not isinstance(extend, str):
                raise ValueError('extend must be a string')
            if len(extend.encode('utf-8')) > 4096:
                raise ValueError('extend is too large')
        return ErrorEventIn(
            message_id=MessageId(message_id),
            timestamp=Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
            error_code=int(payload['errorCode']),
            trigger_time=Timestamp.from_timestamp_epoch_ms(
                int(payload['triggerTime'])
            ),
            extend=extend,
        )

@dataclass
class GrainOutputEventOut:
    message_id: MessageId
    timestamp: Timestamp
    code: Code
    exec_step: ExecStep

    @staticmethod
    def create(**kwargs) -> GrainOutputEventOut:
        return GrainOutputEventOut(
            timestamp = Timestamp.now(),
            **kwargs
        )

    def to_mqtt_payload(self) -> dict:
        return {
            'cmd': Commands.GRAIN_OUTPUT_EVENT,
            'msgId': self.message_id.data,
            'ts': self.timestamp.to_timestamp_epoch_ms(),
            'code': self.code.value,
            'execStep': self.exec_step.name,
        }

@dataclass
class AttrPushEventIn:
    message_id: MessageId
    timestamp: Timestamp

    # Power state
    power_mode: Optional[PowerMode] = None
    power_type: Optional[PowerType] = None
    electric_quantity: Optional[PercentageInt] = None

    # Feeder state
    surplus_grain: Optional[bool] = None
    motor_state: Optional[int] = None
    grain_outlet_state: Optional[bool] = None

    # Network telemetry is optional in sparse push events.
    wifi_ssid: Optional[str] = None

    # Audio playback
    enable_audio: Optional[bool] = None
    audio_url: Optional[str] = None # max length 100
    # Also applies to sound output (volume)
    volume: Optional[PercentageInt] = None

    # Control button lights
    light_switch: Optional[bool] = None
    enable_light: Optional[bool] = None
    disable_hardware_button: Optional[bool] = None
    light_aging_type: Optional[AgingType] = None
    lighting_start_time_utc: Optional[HourMinTimestamp] = None
    lighting_end_time_utc: Optional[HourMinTimestamp] = None
    lighting_times: Optional[int] = None

    # Sound output
    sound_switch: Optional[bool] = None
    enable_sound: Optional[bool] = None
    sound_aging_type: Optional[AgingType] = None
    sound_start_time_utc: Optional[HourMinTimestamp] = None
    sound_end_time_utc: Optional[HourMinTimestamp] = None
    sound_times: Optional[int] = None

    # auto lock buttons?
    auto_change_mode: Optional[bool] = None
    auto_threshold: Optional[int] = None
    bowl_mode: Optional[BowlMode] = None

    # Camera
    camera_switch: Optional[bool] = None
    enable_camera: Optional[bool] = None
    camera_aging_type: Optional[AgingType] = None
    night_vision: Optional[NightVision] = None
    resolution: Optional[Resolution] = None
    camera_start_time_utc: Optional[HourMinTimestamp] = None
    camera_end_time_utc: Optional[HourMinTimestamp] = None

    # Video recording
    video_record_switch: Optional[bool] = None
    enable_video_record: Optional[bool] = None
    sd_card_state: Optional[SdCardState] = None
    sd_card_file_system: Optional[SdCardFileSystem] = None
    sd_card_total_capacity: Optional[int] = None
    sd_card_used_capacity: Optional[int] = None
    video_record_mode: Optional[VideoRecordMode] = None
    video_record_aging_type: Optional[AgingType] = None
    video_record_start_time_utc: Optional[HourMinTimestamp] = None
    video_record_end_time_utc: Optional[HourMinTimestamp] = None

    # Feeding video
    feeding_video_switch: Optional[bool] = None
    enable_video_start_feeding_plan: Optional[bool] = None
    enable_video_after_manual_feeding: Optional[bool] = None
    before_feeding_plan_time: Optional[int] = None # time in seconds
    automatic_recording: Optional[int] = None
    after_manual_feeding_time: Optional[int] = None # time in seconds
    video_watermark_switch: Optional[bool] = None

    # Cloud video recording
    cloud_video_record_switch: Optional[bool] = None
    # Saw these in my message dumps when using the official app but not in the firmware
    # cloud_video_record_mode: str = None
    # cloud_video_recording_aging_type: int = None

    # Motion detection
    motion_detection_switch: Optional[bool] = None
    enable_motion_detection: Optional[bool] = None
    motion_detection_aging_type: Optional[AgingType] = None
    motion_detection_range: Optional[MotionDetectionRange] = None
    motion_detection_sensitivity: Optional[MotionDetectionSensitivity] = None
    motion_detection_start_time_utc: Optional[HourMinTimestamp] = None
    motion_detection_end_time_utc: Optional[HourMinTimestamp] = None

    # Sound detection
    sound_detection_switch: Optional[bool] = None
    enable_sound_detection: Optional[bool] = None
    sound_detection_aging_type: Optional[AgingType] = None
    sound_detection_sensitivity: Optional[SoundDetectionSensitivity] = None
    sound_detection_start_time_utc: Optional[HourMinTimestamp] = None
    sound_detection_end_time_utc: Optional[HourMinTimestamp] = None

    # Preserve the sparse wire payload for forward-compatible field handling.
    # Keep it out of repr/error formatting because it can contain credentials.
    raw_fields: Mapping[str, object] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )

    def has(self, raw_field: str) -> bool:
        """Return whether the original wire payload contained a field."""

        return raw_field in self.raw_fields

    def get(self, raw_field: str, default=None):
        """Read a raw wire field without assuming it exists."""

        return self.raw_fields.get(raw_field, default)

    @staticmethod
    def from_mqtt_payload(payload: dict) -> AttrPushEventIn:
        # at least a very sparse payload. a full payload is sent once
        # on startup but then only stuff that changed (maybe with a few
        # static ones?). just treat the whole structure sparse to avoid
        # any pitfalls here
        data = {
            'message_id': MessageId(payload['msgId']),
            'timestamp': Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
            'raw_fields': dict(payload),
        }

        if 'powerMode' in payload:
            data = data | { 'power_mode': PowerMode(int(payload['powerMode'])) }
        if 'powerType' in payload:
            data = data | { 'power_type': PowerType(int(payload['powerType'])) }
        if 'electricQuantity' in payload:
            data = data | { 'electric_quantity': PercentageInt(int(payload['electricQuantity'])) }

        if 'surplusGrain' in payload:
            data = data | { 'surplus_grain': payload['surplusGrain'] }
        if 'motorState' in payload:
            data = data | { 'motor_state': int(payload['motorState']) }
        if 'grainOutletState' in payload:
            data = data | { 'grain_outlet_state': payload['grainOutletState'] }

        if 'wifiSsid' in payload:
            data = data | { 'wifi_ssid': str(payload['wifiSsid']) }

        if 'enableAudio' in payload:
            data = data | { 'enable_audio': payload['enableAudio'] }
        if 'audioUrl' in payload:
            data = data | { 'audio_url': payload['audioUrl'] }
        if 'volume' in payload:
            data = data | { 'volume': PercentageInt(payload['volume']) }

        if 'lightSwitch' in payload:
            data = data | { 'light_switch': payload['lightSwitch'] }
        if 'enableLight' in payload:
            data = data | { 'enable_light': payload['enableLight'] }
        if 'disableHardwareButton' in payload:
            data = data | { 'disable_hardware_button': payload['disableHardwareButton'] }
        if 'lightAgingType' in payload:
            data = data | { 'light_aging_type': AgingType(payload['lightAgingType']) }
        if 'lightingStartTimeUtc' in payload:
            data = data | { 'lighting_start_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['lightingStartTimeUtc']) }
        if 'lightingEndTimeUtc' in payload:
            data = data | { 'lighting_end_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['lightingEndTimeUtc']) }
        if 'lightingTimes' in payload:
            data = data | { 'lighting_times': int(payload['lightingTimes']) }


        if 'soundSwitch' in payload:
            data = data | { 'sound_switch': payload['soundSwitch'] }
        if 'enableSound' in payload:
            data = data | { 'enable_sound': payload['enableSound'] }
        if 'soundAgingType' in payload:
            data = data | { 'sound_aging_type': AgingType(payload['soundAgingType']) }
        if 'soundStartTimeUtc' in payload:
            data = data | { 'sound_start_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['soundStartTimeUtc']) }
        if 'soundEndTimeUtc' in payload:
            data = data | { 'sound_end_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['soundEndTimeUtc']) }
        if 'soundTimes' in payload:
            data = data | { 'sound_times': payload['soundTimes'] }

        if 'autoChangeMode' in payload:
            data = data | { 'auto_change_mode': payload['autoChangeMode'] }
        if 'autoThreshold' in payload:
            data = data | { 'auto_threshold': payload['autoThreshold'] }
        if 'bowlMode' in payload:
            data = data | { 'bowl_mode': BowlMode.from_mqtt_payload_value(payload['bowlMode']) }

        if 'cameraSwitch' in payload:
            data = data | { 'camera_switch': payload['cameraSwitch'] }
        if 'enableCamera' in payload:
            data = data | { 'enable_camera': payload['enableCamera'] }
        if 'cameraAgingType' in payload:
            data = data | { 'camera_aging_type': AgingType(payload['cameraAgingType']) }
        if 'nightVision' in payload:
            data = data | { 'night_vision': NightVision[payload['nightVision']] }
        if 'resolution' in payload:
            data = data | { 'resolution': Resolution[payload['resolution']] }
        if 'cameraStartTimeUtc' in payload:
            data = data | { 'camera_start_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['cameraStartTimeUtc']) }
        if 'cameraEndTimeUtc' in payload:
            data = data | { 'camera_end_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['cameraEndTimeUtc']) }

        if 'videoRecordSwitch' in payload:
            data = data | { 'video_record_switch': payload['videoRecordSwitch'] }
        if 'enableVideoRecord' in payload:
            data = data | { 'enable_video_record': payload['enableVideoRecord'] }
        if 'sdCardState' in payload:
            data = data | { 'sd_card_state': SdCardState(payload['sdCardState']) }
        if 'sdCardFileSystem' in payload:
            data = data | { 'sd_card_file_system': SdCardFileSystem.from_mqtt_payload_value(payload['sdCardFileSystem']) }
        if 'sdCardTotalCapacity' in payload:
            data = data | { 'sd_card_total_capacity': payload['sdCardTotalCapacity'] }
        if 'sdCardUsedCapacity' in payload:
            data = data | { 'sd_card_used_capacity': payload['sdCardUsedCapacity'] }

        if 'videoRecordMode' in payload:
            data = data | { 'video_record_mode': VideoRecordMode[payload['videoRecordMode']] }
        if 'videoRecordAgingType' in payload:
            data = data | { 'video_record_aging_type': AgingType(payload['videoRecordAgingType']) }
        if 'videoRecordStartTimeUtc' in payload:
            data = data | { 'video_record_start_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['videoRecordStartTimeUtc']) }
        if 'videoRecordEndTimeUtc' in payload:
            data = data | { 'video_record_end_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['videoRecordEndTimeUtc']) }

        if 'feedingVideoSwitch' in payload:
            data = data | { 'feeding_video_switch': payload['feedingVideoSwitch'] }
        if 'enableVideoStartFeedingPlan' in payload:
            data = data | { 'enable_video_start_feeding_plan': payload['enableVideoStartFeedingPlan'] }
        if 'enableVideoAfterManualFeeding' in payload:
            data = data | { 'enable_video_after_manual_feeding': payload['enableVideoAfterManualFeeding'] }
        if 'beforeFeedingPlanTime' in payload:
            data = data | { 'before_feeding_plan_time': payload['beforeFeedingPlanTime'] }
        if 'automaticRecording' in payload:
            data = data | { 'automatic_recording': payload['automaticRecording'] }
        if 'afterManualFeedingTime' in payload:
            data = data | { 'after_manual_feeding_time': payload['afterManualFeedingTime'] }
        if 'videoWatermarkSwitch' in payload:
            data = data | { 'video_watermark_switch': payload['videoWatermarkSwitch'] }

        if 'cloudVideoRecordSwitch' in payload:
            data = data | { 'cloud_video_record_switch': payload['cloudVideoRecordSwitch'] }
        # if 'cloudVideoRecordMode' in payload:
        #     data = data | { 'cloud_video_record_mode': payload['cloudVideoRecordMode'] }
        # if 'cloudVideoRecordAgingType' in payload:
        #     data = data | { 'cloud_video_recording_aging_type': payload['cloudVideoRecordAgingType'] }

        if 'motionDetectionSwitch' in payload:
            data = data | { 'motion_detection_switch': payload['motionDetectionSwitch'] }
        if 'enableMotionDetection' in payload:
            data = data | { 'enable_motion_detection': payload['enableMotionDetection'] }
        if 'motionDetectionAgingType' in payload:
            data = data | { 'motion_detection_aging_type': AgingType(payload['motionDetectionAgingType']) }
        if 'motionDetectionRange' in payload:
            data = data | { 'motion_detection_range': MotionDetectionRange[payload['motionDetectionRange']] }
        if 'motionDetectionSensitivity' in payload:
            data = data | { 'motion_detection_sensitivity': MotionDetectionSensitivity[payload['motionDetectionSensitivity']] }
        if 'motionDetectionStartTimeUtc' in payload:
            data = data | { 'motion_detection_start_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['motionDetectionStartTimeUtc']) }
        if 'motionDetectionEndTimeUtc' in payload:
            data = data | { 'motion_detection_end_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['motionDetectionEndTimeUtc']) }

        if 'soundDetectionSwitch' in payload:
            data = data | { 'sound_detection_switch': payload['soundDetectionSwitch'] }
        if 'enableSoundDetection' in payload:
            data = data | { 'enable_sound_detection': payload['enableSoundDetection'] }
        if 'soundDetectionAgingType' in payload:
            data = data | { 'sound_detection_aging_type': AgingType(payload['soundDetectionAgingType']) }
        if 'soundDetectionSensitivity' in payload:
            data = data | { 'sound_detection_sensitivity': SoundDetectionSensitivity[payload['soundDetectionSensitivity']] }
        if 'soundDetectionStartTimeUtc' in payload:
            data = data | { 'sound_detection_start_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['soundDetectionStartTimeUtc']) }
        if 'soundDetectionEndTimeUtc' in payload:
            data = data | { 'sound_detection_end_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['soundDetectionEndTimeUtc']) }

        return AttrPushEventIn(**data)

@dataclass
class AttrPushEventOut:
    message_id: MessageId
    timestamp: Timestamp
    code: Code

    @staticmethod
    def create(**kwargs) -> AttrPushEventOut:
        return AttrPushEventOut(
            timestamp = Timestamp.now(),
            **kwargs
        )

    def to_mqtt_payload(self) -> dict:
        return {
            'cmd': Commands.ATTR_PUSH_EVENT,
            'msgId': self.message_id.data,
            'ts': self.timestamp.to_timestamp_epoch_ms(),
            'code': self.code.value,
        }

@dataclass
class AttrSetServiceIn:
    message_id: MessageId
    timestamp: Timestamp
    code: Code

    @staticmethod
    def from_mqtt_payload(payload: dict) -> AttrSetServiceIn:
        return AttrSetServiceIn(
            message_id = MessageId(payload['msgId']),
            timestamp = Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
            code = Code(int(payload['code'])),
        )

@dataclass
class AttrSetServiceOut:
    message_id: MessageId
    timestamp: Timestamp

    # Power related stuff
    power_mode: Optional[int] = None

    # Audio playback
    enable_audio: Optional[bool] = None
    audio_url: Optional[str] = None # max length 100
    # Also applies to sound output (volume)
    volume: Optional[PercentageInt] = None

    # Camera
    camera_switch: Optional[bool] = None
    camera_aging_type: Optional[AgingType] = None
    night_vision: Optional[NightVision] = None
    resolution: Optional[Resolution] = None
    camera_start_time_utc: Optional[HourMinTimestamp] = None
    camera_end_time_utc: Optional[HourMinTimestamp] = None

    # Video recording
    video_record_switch: Optional[bool] = None
    video_record_mode: Optional[VideoRecordMode] = None
    video_record_aging_type: Optional[AgingType] = None
    video_record_start_time_utc: Optional[HourMinTimestamp] = None
    video_record_end_time_utc: Optional[HourMinTimestamp] = None

    # Feeding video
    feeding_video_switch: Optional[bool] = None
    enable_video_start_feeding_plan: Optional[bool] = None
    enable_video_after_manual_feeding: Optional[bool] = None
    before_feeding_plan_time: Optional[int] = None # time in seconds
    automatic_recording: Optional[int] = None
    after_manual_feeding_time: Optional[int] = None # time in seconds
    video_watermark_switch: Optional[bool] = None

    # Cloud video recording
    cloud_video_record_switch: Optional[bool] = None
    # Saw these in my message dumps when using the official app but not in the firmware
    # cloud_video_record_mode: str = None
    # cloud_video_recording_aging_type: int = None

    # Motion detection
    motion_detection_switch: Optional[bool] = None
    motion_detection_aging_type: Optional[AgingType] = None
    motion_detection_range: Optional[MotionDetectionRange] = None
    motion_detection_sensitivity: Optional[MotionDetectionSensitivity] = None
    motion_detection_start_time_utc: Optional[HourMinTimestamp] = None
    motion_detection_end_time_utc: Optional[HourMinTimestamp] = None

    # Sound detection
    sound_detection_switch: Optional[bool] = None
    sound_detection_aging_type: Optional[AgingType] = None
    sound_detection_sensitivity: Optional[SoundDetectionSensitivity] = None
    sound_detection_start_time_utc: Optional[HourMinTimestamp] = None
    sound_detection_end_time_utc: Optional[HourMinTimestamp] = None

    # Sound output
    sound_switch: Optional[bool] = None
    sound_aging_type: Optional[AgingType] = None
    sound_start_time_utc: Optional[HourMinTimestamp] = None
    sound_end_time_utc: Optional[HourMinTimestamp] = None
    sound_times: Optional[int] = None

    # Control button lights
    light_switch: Optional[bool] = None
    light_aging_type: Optional[AgingType] = None
    lighting_start_time_utc: Optional[HourMinTimestamp] = None
    lighting_end_time_utc: Optional[HourMinTimestamp] = None
    lighting_times: Optional[int] = None

    # auto lock buttons?
    auto_change_mode: Optional[bool] = None
    auto_threshold: Optional[int] = None

    # Physical food-tray configuration
    bowl_mode: Optional[BowlMode] = None

    @staticmethod
    def create(**kwargs) -> AttrSetServiceOut:
        return AttrSetServiceOut(
            message_id = MessageId.generate(),
            timestamp = Timestamp.now(),
            **kwargs
        )

    def to_mqtt_payload(self) -> dict:
        payload = {
            'cmd': Commands.ATTR_SET_SERVICE,
            'msgId': self.message_id.data,
            'ts': self.timestamp.to_timestamp_epoch_ms(),
        }

        # Audio playback
        if not self.enable_audio == None:
            payload = payload | { 'enableAudio': self.enable_audio }
        if not self.audio_url == None:
            payload = payload | { 'audioUrl': self.audio_url }
        if not self.volume == None:
            payload = payload | { 'volume': self.volume.value_get() }

        # Camera
        if not self.camera_switch == None:
            payload = payload | { 'cameraSwitch': self.camera_switch }
        if not self.camera_aging_type == None:
            payload = payload | { 'cameraAgingType': self.camera_aging_type.value }
        if not self.night_vision == None:
            payload = payload | { 'nightVision': self.night_vision.name }
        if not self.resolution == None:
            payload = payload | { 'resolution': self.resolution.name }
        if not self.camera_start_time_utc == None:
            payload = payload | { 'cameraStartTimeUtc': self.camera_start_time_utc.to_mqtt_payload_value() }
        if not self.camera_end_time_utc == None:
            payload = payload | { 'cameraEndTimeUtc': self.camera_end_time_utc.to_mqtt_payload_value() }

        # Video recording
        if not self.video_record_switch == None:
            payload = payload | { 'videoRecordSwitch': self.video_record_switch }
        if not self.video_record_mode == None:
            payload = payload | { 'videoRecordMode': self.video_record_mode.name }
        if not self.video_record_aging_type == None:
            payload = payload | { 'videoRecordAgingType': self.video_record_aging_type.value }
        if not self.video_record_start_time_utc == None:
            payload = payload | { 'videoRecordStartTimeUtc': self.video_record_start_time_utc.to_mqtt_payload_value() }
        if not self.video_record_end_time_utc == None:
            payload = payload | { 'videoRecordEndTimeUtc': self.video_record_end_time_utc.to_mqtt_payload_value() }

        # Feeding video
        if not self.feeding_video_switch == None:
            payload = payload | { 'feedingVideoSwitch': self.feeding_video_switch }
        if not self.enable_video_start_feeding_plan == None:
            payload = payload | { 'enableVideoStartFeedingPlan': self.enable_video_start_feeding_plan }
        if not self.after_manual_feeding_time == None:
            payload = payload | { 'afterManualFeedingTime': self.after_manual_feeding_time }
        if not self.before_feeding_plan_time == None:
            payload = payload | { 'beforeFeedingPlanTime': self.before_feeding_plan_time }
        if not self.automatic_recording == None:
            payload = payload | { 'automaticRecording': self.automatic_recording }
        if not self.enable_video_after_manual_feeding == None:
            payload = payload | { 'enableVideoAfterManualFeeding': self.enable_video_after_manual_feeding }
        if not self.video_watermark_switch == None:
            payload = payload | { 'videoWatermarkSwitch': self.video_watermark_switch }

        # Cloud video recording
        if not self.cloud_video_record_switch == None:
            payload = payload | { 'cloudVideoRecordSwitch': self.cloud_video_record_switch }
        # if not self.cloud_video_record_mode == None:
        #     payload = payload | { 'cloudVideoRecordMode': self.cloud_video_record_mode }
        # if not self.cloud_video_recording_aging_type == None:
        #     payload = payload | { 'cloudVideoRecordingAgingType': self.cloud_video_recording_aging_type }

        # Motion detection
        if not self.motion_detection_switch == None:
            payload = payload | { 'motionDetectionSwitch': self.motion_detection_switch }
        if not self.motion_detection_aging_type == None:
            payload = payload | { 'motionDetectionAgingType': self.motion_detection_aging_type.value }
        if not self.motion_detection_range == None:
            payload = payload | { 'motionDetectionRange': self.motion_detection_range.name }
        if not self.motion_detection_sensitivity == None:
            payload = payload | { 'motionDetectionSensitivity': self.motion_detection_sensitivity.name }
        if not self.motion_detection_start_time_utc == None:
            payload = payload | { 'motionDetectionStartTimeUtc': self.motion_detection_start_time_utc.to_mqtt_payload_value() }
        if not self.motion_detection_end_time_utc == None:
            payload = payload | { 'motionDetectionEndTimeUtc': self.motion_detection_end_time_utc.to_mqtt_payload_value() }

        # Sound detection
        if not self.sound_detection_switch == None:
            payload = payload | { 'soundDetectionSwitch': self.sound_detection_switch }
        if not self.sound_detection_aging_type == None:
            payload = payload | { 'soundDetectionAgingType': self.sound_detection_aging_type.value }
        if not self.sound_detection_sensitivity == None:
            payload = payload | { 'soundDetectionSensitivity': self.sound_detection_sensitivity.name }
        if not self.sound_detection_start_time_utc == None:
            payload = payload | { 'soundDetectionStartTimeUtc': self.sound_detection_start_time_utc.to_mqtt_payload_value() }
        if not self.sound_detection_end_time_utc == None:
            payload = payload | { 'soundDetectionEndTimeUtc': self.sound_detection_end_time_utc.to_mqtt_payload_value() }

        # Sound output
        if not self.sound_switch == None:
            payload = payload | { 'soundSwitch': self.sound_switch }
        if not self.sound_aging_type == None:
            payload = payload | { 'soundAgingType': self.sound_aging_type.value }
        if not self.sound_start_time_utc == None:
            payload = payload | { 'soundStartTimeUtc': self.sound_start_time_utc.to_mqtt_payload_value() }
        if not self.sound_end_time_utc == None:
            payload = payload | { 'soundEndTimeUtc': self.sound_end_time_utc.to_mqtt_payload_value() }
        if not self.sound_times == None:
            payload = payload | { 'soundTimes': self.sound_times }

        # Control button lights
        if not self.light_switch == None:
            payload = payload | { 'lightSwitch': self.light_switch }
        if not self.light_aging_type == None:
            payload = payload | { 'lightAgingType': self.light_aging_type.value }
        if not self.lighting_start_time_utc == None:
            payload = payload | { 'lightingStartTimeUtc': self.lighting_start_time_utc.to_mqtt_payload_value() }
        if not self.lighting_end_time_utc == None:
            payload = payload | { 'lightingEndTimeUtc': self.lighting_end_time_utc.to_mqtt_payload_value() }
        if not self.lighting_times == None:
            payload = payload | { 'lightingTimes': self.lighting_times }

        # auto lock buttons?
        if not self.auto_change_mode == None:
            payload = payload | { 'autoChangeMode': self.auto_change_mode }
        if not self.auto_threshold == None:
            payload = payload | { 'autoThreshold': self.auto_threshold }

        if not self.bowl_mode == None:
            payload = payload | { 'bowlMode': self.bowl_mode.value }

        return payload

@dataclass
class FeedingPlanIn:
    plan_id: int
    sync_time: Timestamp

@dataclass
class FeedingPlanServiceIn:
    message_id: MessageId
    timestamp: Timestamp
    code: Code
    plans: list[FeedingPlanIn]
    # Only available on error, can be either "MsgErro" or "FeedPlanErro"
    msg: Optional[str] = None

    @staticmethod
    def from_mqtt_payload(payload: dict) -> FeedingPlanServiceIn:
        plans: list[dict] = payload['plans']
        plans_data: list[FeedingPlanIn] = []
        msg: str = None

        for plan in plans:
            plans_data.append(FeedingPlanIn(
                plan_id = plan['planId'],
                sync_time = Timestamp.from_timestamp_epoch_ms(int(plan['syncTime']))
            ))

        if 'msg' in payload:
            msg = payload['msg']

        return FeedingPlanServiceIn(
            message_id = MessageId(payload['msgId']),
            timestamp = Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
            code = Code(int(payload['code'])),
            msg = msg,
            plans = plans_data,
        )

@dataclass
class FeedingPlanOut:
    plan_id: int
    execution_time: HourMinTimestamp
    repeat_day: WeekdaySchedule
    enable_audio: bool
    audio_times: int
    grain_num: int
    sync_time: Timestamp
    skip_end_time: Optional[int] = None

@dataclass
class FeedingPlanServiceOut:
    message_id: MessageId
    timestamp: Timestamp
    plans: list[FeedingPlanOut]

    @staticmethod
    def create(plans: list[FeedingPlanOut]) -> FeedingPlanServiceOut:
        return FeedingPlanServiceOut(
            message_id = MessageId.generate(),
            timestamp = Timestamp.now(),
            plans = plans,
        )

    def to_mqtt_payload(self) -> dict:
        plans: list[dict] = []

        for plan in self.plans:
            plan_payload = {
                'planId': plan.plan_id,
                'executionTime': plan.execution_time.to_mqtt_payload_value(),
                'repeatDay': plan.repeat_day.to_mqtt_payload_value(),
                'enableAudio': plan.enable_audio,
                'audioTimes': plan.audio_times,
                'grainNum': plan.grain_num,
                'syncTime': plan.sync_time.to_timestamp_epoch_ms(),
            }
            if plan.skip_end_time is not None:
                plan_payload['skipEndTime'] = plan.skip_end_time
            plans.append(plan_payload)

        return {
            'cmd': Commands.FEEDING_PLAN_SERVICE,
            'msgId': self.message_id.data,
            'ts': self.timestamp.to_timestamp_epoch_ms(),
            'plans': plans,
        }

@dataclass
class GetFeedingPlanEventIn:
    message_id: MessageId
    timestamp: Timestamp

    @staticmethod
    def from_mqtt_payload(payload: dict) -> GetFeedingPlanEventIn:
        return GetFeedingPlanEventIn(
            message_id = MessageId(payload['msgId']),
            timestamp = Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
        )

@dataclass
class GetFeedingPlanOut:
    plan_id: int
    execution_time: HourMinTimestamp
    repeat_day: WeekdaySchedule
    enable_audio: bool
    audio_times: int
    grain_num: int
    sync_time: Timestamp
    skip_end_time: Optional[int] = None

@dataclass
class GetFeedingPlanEventOut:
    message_id: MessageId
    timestamp: Timestamp
    code: Code
    plans: list[GetFeedingPlanOut]

    @staticmethod
    def create(code: Code, plans: list[GetFeedingPlanOut]) -> GetFeedingPlanEventOut:
        return GetFeedingPlanEventOut(
            message_id = MessageId.generate(),
            timestamp = Timestamp.now(),
            code = code,
            plans = plans,
        )

    def to_mqtt_payload(self) -> dict:
        plans: list[dict] = []

        for plan in self.plans:
            if plan.skip_end_time == None:
                plans.append({
                    'planId': plan.plan_id,
                    'executionTime': plan.execution_time.to_mqtt_payload_value(),
                    'repeatDay': plan.repeat_day.to_mqtt_payload_value(),
                    'enableAudio': plan.enable_audio,
                    'audioTimes': plan.audio_times,
                    'grainNum': plan.grain_num,
                    'syncTime': plan.sync_time.to_timestamp_epoch_ms(),
                })
            else:
                plans.append({
                    'planId': plan.plan_id,
                    'executionTime': plan.execution_time.to_mqtt_payload_value(),
                    'repeatDay': plan.repeat_day.to_mqtt_payload_value(),
                    'enableAudio': plan.enable_audio,
                    'audioTimes': plan.audio_times,
                    'grainNum': plan.grain_num,
                    'syncTime': plan.sync_time.to_timestamp_epoch_ms(),
                    'skipEndTime': plan.skip_end_time,
                })

        return {
            'cmd': Commands.GET_FEEDING_PLAN_EVENT,
            'msgId': self.message_id.data,
            'ts': self.timestamp.to_timestamp_epoch_ms(),
            'code': self.code.value,
            'plans': plans,
        }












@dataclass
class GetConfigIn:
    message_id: MessageId
    timestamp: Timestamp
    product_id: str
    mac_address: str
    hardware_version: str
    software_version: str

    @staticmethod
    def from_mqtt_payload(payload: dict) -> GetConfigIn:
        return GetConfigIn(
            message_id = MessageId(payload['msgId']),
            timestamp = Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
            product_id = payload['pid'],
            mac_address = payload['mac'],
            hardware_version = payload['hardwareVersion'],
            software_version = payload['softwareVersion'],
        )

@dataclass
class GetConfigOut:
    message_id: MessageId
    timestamp: Timestamp

    @staticmethod
    def create() -> GetConfigOut:
        return GetConfigOut(
            message_id = MessageId.generate(),
            timestamp = Timestamp.now(),
        )

    def to_mqtt_payload(self) -> dict:
        return {
            'cmd': Commands.GET_CONFIG,
            'msgId': self.message_id.data,
            'ts': self.timestamp.to_timestamp_epoch_ms(),
        }

@dataclass
class AttrGetServiceIn:
    message_id: MessageId
    timestamp: Timestamp
    code: Code

    # Power state
    power_mode: PowerMode
    power_type: PowerType
    electric_quantity: PercentageInt

    # Feeder state
    surplus_grain: bool
    motor_state: int
    grain_outlet_state: bool

    # Wifi
    wifi_ssid: str

    # Audio playback
    enable_audio: bool
    audio_url: str
    # Also applies to sound output (volume)
    volume: PercentageInt

    # Control button lights
    enable_light: bool
    light_switch: bool
    light_aging_type: AgingType

    # Sound output
    enable_sound: bool
    sound_switch: bool
    sound_aging_type: AgingType

    # Automatic button lock?
    auto_change_mode: bool
    auto_threshold: int

    # Camera
    camera_switch: bool
    enable_camera: bool
    camera_aging_type: AgingType
    resolution: Resolution
    night_vision: NightVision

    # Video recording
    video_record_switch: bool
    enable_video_record: bool
    sd_card_state: SdCardState
    video_record_mode: VideoRecordMode
    video_record_aging_type: AgingType

    # Feeding video
    feeding_video_switch: bool
    enable_video_start_feeding_plan: bool
    enable_video_after_manual_feeding: bool
    before_feeding_plan_time: int
    automatic_recording: int
    after_manual_feeding_time: int
    video_watermark_switch: bool

    # Cloud video recording
    cloud_video_record_switch: bool

    # Motion detection
    motion_detection_switch: bool
    enable_motion_detection: bool
    motion_detection_aging_type: AgingType
    motion_detection_sensitivity: MotionDetectionSensitivity
    motion_detection_range: MotionDetectionRange

    # Sound detection
    sound_detection_switch: bool
    enable_sound_detection: bool
    sound_detection_aging_type: AgingType
    sound_detection_sensitivity: SoundDetectionSensitivity

    ### Optionals

    # Control button lights
    lighting_start_time_utc: Optional[HourMinTimestamp] = None
    lighting_end_time_utc: Optional[HourMinTimestamp] = None
    lighting_times: Optional[int] = None

    # Sound output
    sound_start_time_utc: Optional[HourMinTimestamp] = None
    sound_end_time_utc: Optional[HourMinTimestamp] = None
    sound_times: Optional[int] = None

    # Camera
    camera_start_time_utc: Optional[HourMinTimestamp] = None
    camera_end_time_utc: Optional[HourMinTimestamp] = None

    # Video recording
    sd_card_file_system: Optional[SdCardFileSystem] = None
    sd_card_total_capacity: Optional[int] = None
    sd_card_used_capacity: Optional[int] = None
    video_record_start_time_utc: Optional[HourMinTimestamp] = None
    video_record_end_time_utc: Optional[HourMinTimestamp] = None

    # Motion detection
    motion_detection_start_time_utc: Optional[HourMinTimestamp] = None
    motion_detection_end_time_utc: Optional[HourMinTimestamp] = None

    # Sound detection
    sound_detection_start_time_utc: Optional[HourMinTimestamp] = None
    sound_detection_end_time_utc: Optional[HourMinTimestamp] = None

    # Physical food-tray configuration
    bowl_mode: Optional[BowlMode] = None

    @staticmethod
    def from_mqtt_payload(payload: dict) -> AttrGetServiceIn:
        data = {
            'message_id': MessageId(payload['msgId']),
            'timestamp': Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
            'code': Code(int(payload['code'])),

            'power_mode': PowerMode(int(payload['powerMode'])),
            'power_type': PowerType(int(payload['powerType'])),
            'electric_quantity': PercentageInt(int(payload['electricQuantity'])),

            'surplus_grain': payload['surplusGrain'],
            'motor_state': int(payload['motorState']),
            'grain_outlet_state': payload['grainOutletState'],

            'wifi_ssid': payload['wifiSsid'],

            'enable_audio': False if int(payload['enableAudio']) == 0 else True,
            'audio_url': payload['audioUrl'],
            'volume': PercentageInt(int(payload['volume'])),

            'enable_light': payload['enableLight'],
            'light_switch': payload['lightSwitch'],
            'light_aging_type': AgingType(int(payload['lightAgingType'])),

            'enable_sound': payload['enableSound'],
            'sound_switch': payload['soundSwitch'],
            'sound_aging_type': AgingType(int(payload['soundAgingType'])),

            'auto_change_mode': payload['autoChangeMode'],
            'auto_threshold': int(payload['autoThreshold']),

            'camera_switch': payload['cameraSwitch'],
            'enable_camera': payload['enableCamera'],
            'camera_aging_type': AgingType(int(payload['cameraAgingType'])),
            'resolution': Resolution[payload['resolution']],
            'night_vision': NightVision[payload['nightVision']],

            'video_record_switch': payload['videoRecordSwitch'],
            'enable_video_record': payload['enableVideoRecord'],
            'sd_card_state': SdCardState(int(payload['sdCardState'])),
            'video_record_mode': VideoRecordMode[payload['videoRecordMode']],
            'video_record_aging_type': AgingType(int(payload['videoRecordAgingType'])),

            'feeding_video_switch': payload['feedingVideoSwitch'],
            'enable_video_start_feeding_plan': payload['enableVideoStartFeedingPlan'],
            'enable_video_after_manual_feeding': payload['enableVideoAfterManualFeeding'],
            'before_feeding_plan_time': int(payload['beforeFeedingPlanTime']),
            'automatic_recording': int(payload['automaticRecording']),
            'after_manual_feeding_time': int(payload['afterManualFeedingTime']),
            'video_watermark_switch': payload['videoWatermarkSwitch'],

            'cloud_video_record_switch': payload['cloudVideoRecordSwitch'],

            'motion_detection_switch': payload['motionDetectionSwitch'],
            'enable_motion_detection': payload['enableMotionDetection'],
            'motion_detection_aging_type': AgingType(int(payload['motionDetectionAgingType'])),
            'motion_detection_sensitivity': MotionDetectionSensitivity[payload['motionDetectionSensitivity']],
            'motion_detection_range': MotionDetectionRange[payload['motionDetectionRange']],

            'sound_detection_switch': payload['soundDetectionSwitch'],
            'enable_sound_detection': payload['enableSoundDetection'],
            'sound_detection_aging_type': AgingType(int(payload['soundDetectionAgingType'])),
            'sound_detection_sensitivity': SoundDetectionSensitivity[payload['soundDetectionSensitivity']],
        }

        if 'lightingStartTimeUtc' in payload:
            data = data | { 'lighting_start_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['lightingStartTimeUtc']) }
        if 'lightingEndTimeUtc' in payload:
            data = data | { 'lighting_end_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['lightingEndTimeUtc']) }
        if 'lightingTimes' in payload:
            data = data | { 'lighting_times': int(payload['lightingTimes']) }

        if 'soundStartTimeUtc' in payload:
            data = data | { 'sound_start_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['soundStartTimeUtc']) }
        if 'soundEndTimeUtc' in payload:
            data = data | { 'sound_end_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['soundEndTimeUtc']) }
        if 'soundTimes' in payload:
            data = data | { 'sound_times': int(payload['soundTimes']) }

        if 'cameraStartTimeUtc' in payload:
            data = data | { 'camera_start_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['cameraStartTimeUtc']) }
        if 'cameraEndTimeUtc' in payload:
            data = data | { 'camera_end_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['cameraEndTimeUtc']) }

        if 'sdCardFileSystem' in payload:
            data = data | { 'sd_card_file_system': SdCardFileSystem.from_mqtt_payload_value(payload['sdCardFileSystem']) }
        if 'sdCardTotalCapacity' in payload:
            data = data | { 'sd_card_total_capacity': int(payload['sdCardTotalCapacity']) }
        if 'sdCardUsedCapacity' in payload:
            data = data | { 'sd_card_used_capacity': int(payload['sdCardUsedCapacity']) }
        if 'videoRecordStartTimeUtc' in payload:
            data = data | { 'video_record_start_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['videoRecordStartTimeUtc']) }
        if 'videoRecordEndTimeUtc' in payload:
            data = data | { 'video_record_end_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['videoRecordEndTimeUtc']) }

        if 'motionDetectionStartTimeUtc' in payload:
            data = data | { 'motion_detection_start_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['motionDetectionStartTimeUtc']) }
        if 'motionDetectionEndTimeUtc' in payload:
            data = data | { 'motion_detection_end_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['motionDetectionEndTimeUtc']) }

        if 'soundDetectionStartTimeUtc' in payload:
            data = data | { 'sound_detection_start_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['soundDetectionStartTimeUtc']) }
        if 'soundDetectionEndTimeUtc' in payload:
            data = data | { 'sound_detection_end_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['soundDetectionEndTimeUtc']) }

        if 'bowlMode' in payload:
            data = data | { 'bowl_mode': BowlMode.from_mqtt_payload_value(payload['bowlMode']) }

        return AttrGetServiceIn(**data)

@dataclass
class AttrGetServiceOut:
    message_id: MessageId
    timestamp: Timestamp

    @staticmethod
    def create() -> AttrGetServiceOut:
        return AttrGetServiceOut(
            message_id = MessageId.generate(),
            timestamp = Timestamp.now(),
        )

    def to_mqtt_payload(self) -> dict:
        return {
            'cmd': Commands.ATTR_GET_SERVICE,
            'msgId': self.message_id.data,
            'ts': self.timestamp.to_timestamp_epoch_ms(),
        }

@dataclass
class WifiReconnectServiceOut:
    message_id: MessageId
    timestamp: Timestamp

    @staticmethod
    def create() -> WifiReconnectServiceOut:
        return WifiReconnectServiceOut(
            message_id = MessageId.generate(),
            timestamp = Timestamp.now(),
        )

    def to_mqtt_payload(self) -> dict:
        return {
            'cmd': Commands.WIFI_RECONNECT_SERVICE,
            'msgId': self.message_id.data,
            'ts': self.timestamp.to_timestamp_epoch_ms()
        }

@dataclass
class WifiReconnectServiceIn:
    message_id: MessageId
    timestamp: Timestamp
    code: Code

    @staticmethod
    def from_mqtt_payload(payload: dict) -> WifiReconnectServiceIn:
        return WifiReconnectServiceIn(
            message_id = MessageId(payload['msgId']),
            timestamp = Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
            code = Code(int(payload['code'])),
        )





@dataclass
class RestoreOut:
    message_id: MessageId
    timestamp: Timestamp

    @staticmethod
    def create() -> RestoreOut:
        return RestoreOut(
            message_id = MessageId.generate(),
            timestamp = Timestamp.now(),
        )

    def to_mqtt_payload(self) -> dict:
        return {
            'cmd': Commands.RESTORE,
            'msgId': self.message_id.data,
            'ts': self.timestamp.to_timestamp_epoch_ms(),
        }

@dataclass
class RestoreIn:
    message_id: MessageId
    timestamp: Timestamp
    code: Code

    @staticmethod
    def from_mqtt_payload(payload: dict) -> RestoreIn:
        return RestoreIn(
            message_id = MessageId(payload['msgId']),
            timestamp = Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
            code = Code(int(payload['code'])),
        )

@dataclass
class InitializeSdCardServiceOut:
    message_id: MessageId
    timestamp: Timestamp

    @staticmethod
    def create() -> InitializeSdCardServiceOut:
        return InitializeSdCardServiceOut(
            message_id = MessageId.generate(),
            timestamp = Timestamp.now(),
        )

    def to_mqtt_payload(self) -> dict:
        return {
            'cmd': Commands.INITIALIZE_SD_CARD_SERVICE,
            'msgId': self.message_id.data,
            'ts': self.timestamp.to_timestamp_epoch_ms(),
        }

@dataclass
class InitializeSdCardServiceIn:
    message_id: MessageId
    timestamp: Timestamp
    code: Code

    @staticmethod
    def from_mqtt_payload(payload: dict) -> InitializeSdCardServiceIn:
        return InitializeSdCardServiceIn(
            message_id = MessageId(payload['msgId']),
            timestamp = Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
            code = Code(int(payload['code'])),
        )

@dataclass
class DeviceRebootOut:
    message_id: MessageId
    timestamp: Timestamp

    @staticmethod
    def create() -> DeviceRebootOut:
        return DeviceRebootOut(
            message_id = MessageId.generate(),
            timestamp = Timestamp.now(),
        )

    def to_mqtt_payload(self) -> dict:
        return {
            'cmd': Commands.DEVICE_REBOOT,
            'msgId': self.message_id.data,
            'ts': self.timestamp.to_timestamp_epoch_ms(),
        }

@dataclass
class DeviceRebootIn:
    message_id: MessageId
    timestamp: Timestamp
    code: Code

    @staticmethod
    def from_mqtt_payload(payload: dict) -> DeviceRebootIn:
        return DeviceRebootIn(
            message_id = MessageId(payload['msgId']),
            timestamp = Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
            code = Code(int(payload['code'])),
        )
