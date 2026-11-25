#!/usr/bin/env python3
"""
vlt_ps2.py — Pure-Python PS2 NFSMW VPAK/VLT parser, inspector & verified recipe patcher
(GLOBAL\\ATTRIBUTES.BIN, GLOBAL\\FE_ATTRIB.BIN, GLOBAL\\GAMEPLAY.BIN).

Standard PC tools (NFS-VltEd, OGVI) struggle with PS2 VPAK vaults and PS2 MemoryFile
packaging. This clean-room tool reads PS2 VPAK containers directly from an untouched ISO
(strictly read-only 'rb') or an extracted .bin file, parses DepN/StrN/DatN/ExpN/PtrN +
.bin (StrE + payload), decodes vault classes, VltCollection nodes, field types, and exact
file byte offsets, and applies declarative JSON override recipes with mandatory
`expected_orig` value verification before modifying a single byte.

Clean-room format reference: NFSTools/VaultLib (MIT) and dbalatoni13/nfsmw (CC0-1.0).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import struct
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from zdir_tool import PS2NFSMWArchive, bstringhash

# ---------------------------------------------------------------------------
# AttribSys / VLT Binary Constants (PS2 SLUS-21351, 32-bit Little-Endian)
# ---------------------------------------------------------------------------

EXPORT_TYPE_DATABASE = 0xCBBC628F    # Attrib::DatabaseLoadData
EXPORT_TYPE_CLASS = 0x5E970CBC       # Attrib::ClassLoadData
EXPORT_TYPE_COLLECTION = 0x8E112EB7  # Attrib::CollectionLoadData

# Attrib::Definition flags (byte at +0x0E)
DEF_FLAG_ARRAY = 0x01
DEF_FLAG_IN_LAYOUT = 0x02
DEF_FLAG_IS_BOUND = 0x04
DEF_FLAG_NOT_SEARCHABLE = 0x08
DEF_FLAG_STATIC = 0x10
DEF_FLAG_HAS_HANDLER = 0x20

# Attrib::Node flags (byte at +0x0A in AttribEntry)
NODE_FLAG_REQUIRES_RELEASE = 0x01
NODE_FLAG_IS_ARRAY = 0x02
NODE_FLAG_IS_INHERITED = 0x04
NODE_FLAG_IS_ACCESSOR = 0x08
NODE_FLAG_IS_LAID_OUT = 0x10
NODE_FLAG_IS_BY_VALUE = 0x20
NODE_FLAG_IS_LOCATABLE = 0x40

# Attrib::Array::Flag_AlignedAt16 (bit 15 of mEncodedTypePad)
ARRAY_FLAG_ALIGNED_16 = 0x8000

# PtrN relocation record types
PTR_END = 0
PTR_NULL = 1
PTR_SET_FIXUP_TARGET = 2
PTR_DEP_RELATIVE = 3
PTR_EXPORT = 4

# EA MemoryFile header magic (GLOBAL\GLOBALMEMORYFILE.BIN)
MEMORY_FILE_MAGIC = 0x53219999


def vlt32_hash(s: str, initval: int = 0xABCDEF00) -> int:
    """EA Black Box VLT32 (Bob Jenkins lookup2 hash, init=0xABCDEF00)."""
    data = s.encode("ascii")
    length = len(data)
    a = b = 0x9E3779B9
    c = initval
    pos = 0

    def mix(a: int, b: int, c: int) -> Tuple[int, int, int]:
        a = (a - b - c) & 0xFFFFFFFF
        a ^= c >> 13
        b = (b - c - a) & 0xFFFFFFFF
        b ^= (a << 8) & 0xFFFFFFFF
        c = (c - a - b) & 0xFFFFFFFF
        c ^= b >> 13
        a = (a - b - c) & 0xFFFFFFFF
        a ^= c >> 12
        b = (b - c - a) & 0xFFFFFFFF
        b ^= (a << 16) & 0xFFFFFFFF
        c = (c - a - b) & 0xFFFFFFFF
        c ^= b >> 5
        a = (a - b - c) & 0xFFFFFFFF
        a ^= c >> 3
        b = (b - c - a) & 0xFFFFFFFF
        b ^= (a << 10) & 0xFFFFFFFF
        c = (c - a - b) & 0xFFFFFFFF
        c ^= b >> 15
        return a, b, c

    rem = length
    while rem >= 12:
        a = (a + struct.unpack_from("<I", data, pos)[0]) & 0xFFFFFFFF
        b = (b + struct.unpack_from("<I", data, pos + 4)[0]) & 0xFFFFFFFF
        c = (c + struct.unpack_from("<I", data, pos + 8)[0]) & 0xFFFFFFFF
        a, b, c = mix(a, b, c)
        pos += 12
        rem -= 12

    c = (c + length) & 0xFFFFFFFF
    if rem >= 11:
        c = (c + (data[pos + 10] << 24)) & 0xFFFFFFFF
    if rem >= 10:
        c = (c + (data[pos + 9] << 16)) & 0xFFFFFFFF
    if rem >= 9:
        c = (c + (data[pos + 8] << 8)) & 0xFFFFFFFF
    if rem >= 8:
        b = (b + (data[pos + 7] << 24)) & 0xFFFFFFFF
    if rem >= 7:
        b = (b + (data[pos + 6] << 16)) & 0xFFFFFFFF
    if rem >= 6:
        b = (b + (data[pos + 5] << 8)) & 0xFFFFFFFF
    if rem >= 5:
        b = (b + data[pos + 4]) & 0xFFFFFFFF
    if rem >= 4:
        a = (a + (data[pos + 3] << 24)) & 0xFFFFFFFF
    if rem >= 3:
        a = (a + (data[pos + 2] << 16)) & 0xFFFFFFFF
    if rem >= 2:
        a = (a + (data[pos + 1] << 8)) & 0xFFFFFFFF
    if rem >= 1:
        a = (a + data[pos]) & 0xFFFFFFFF
    a, b, c = mix(a, b, c)
    return c


# All 56 classes verified present in PS2 SLUS-21351 GLOBAL\ATTRIBUTES.BIN
# plus additional cross-platform NFSMW classes for complete hash lookup.
KNOWN_VLT_CLASSES = [
    # Present in PS2 SLUS-21351 GLOBAL\ATTRIBUTES.BIN (56 classes)
    "acceltrans",
    "aivehicle",
    "aud_moment_strm",
    "aud_stitch_loop",
    "audioimpact",
    "audioscrape",
    "audiosystem",
    "brakes",
    "camerainfo",
    "chassis",
    "chopperspecs",
    "collisionreactions",
    "controller",
    "damagespecs",
    "ecar",
    "effects",
    "emitterdata",
    "emittergroup",
    "emitteruv",
    "engine",
    "engineaudio",
    "explosion",
    "fecooling",
    "frontend",
    "fuelcell_effect",
    "fuelcell_emitter",
    "gameplay",
    "induction",
    "infractions",
    "junkman",
    "light_flares_cg",
    "milestonetypes",
    "music",
    "nos",
    "ocean",
    "presetride",
    "pursuitescalation",
    "pursuitlevels",
    "pursuitsupport",
    "pvehicle",
    "rigidbodyspecs",
    "shiftpattern",
    "simsurface",
    "smackable",
    "speech",
    "speechtune",
    "system",
    "tires",
    "trafficpattern",
    "transmission",
    "turbosfx",
    "visuallook",
    "visuallookeffect",
    "visuallooktransition",
    "visualrgbtweaker",
    "world",
    # Cross-platform / PC-only / legacy aliases
    "timeofdaylighting",
    "lightmaterials",
    "lightshaders",
    "suninfo",
    "visualtreatment",
    "camera",
    "aicop",
    "pursuit",
    "fe_cars",
    "milestones",
    "racers",
    "tracks",
    "weather",
    "effect",
    "collisions",
    "damage",
    "audio",
    "chopper",
]

VLT_CLASS_HASHES: Dict[int, str] = {vlt32_hash(k): k for k in KNOWN_VLT_CLASSES}

# Public-domain (CC0-1.0, dbalatoni13/nfsmw) VLT field, class & collection identifiers
# matched against SLUS-21351 ATTRIBUTES.BIN / FE_ATTRIB.BIN / GAMEPLAY.BIN so every
# class and field resolves cleanly offline with zero external dependencies.
_CC0_VLT_NAMES = """
911turbo_top 997s_base 997s_top ACCUMULATE_SCORES AEMSMix_L_RPM AEMSMix_S_RPM AEMSVol AEMS_EnvBanks
AEMS_FEBanks AEMS_MiscBanks AEMS_NOSBanks AEMS_RNBanks AEMS_SkidBanks AEMS_SparkBanks
AEMS_StitchBanks AEMS_WNBanks AERO_CG AERO_COEFFICIENT AIC_BOND_LGADGET AIC_BOND_RGADGET AIGoalPit
AIGoalRacer AIGoalStaticRoadBlock AIGoalStaticRoadblock AIR_RESISTANCE AIR_RESISTANCE_SCALE
AIRacerProximity AI_AVOIDABLE ALLOW_OFF_WORLD ANGLE ANIM_BANK_NIS_BUFFER ASPECT_RATIO AUTO_SIMPLIFY
AccelDelta AccelDeltaRPMThreshold AccelFromIdle_INTERUPT_T AccelFromIdle_PEAK_RPM
AccelFromIdle_PEAK_T AccelFromIdle_PEAK_VOL AccelFromIdle_RESUME_T AccelStart AccelerationMultiplier
AirSupportOptions Album AllowInvisibleSpawn AlphaToKillAt Artist Aud_RoadNoise_TransOFF
Aud_RoadNoise_TransON Aud_Roadnoise_LOOP Aud_Skid_Type AudioCullDist AudioFX_BOTTOM AudioFX_DEFAULT
AudioFX_FRONT AudioFX_REAR AudioFX_SIDE AudioFX_TOP AudioQuadratic AutoActivateGPS
AutoSpawnTriggerType AutoStart AvailableOnline AvailableQR AxisConstraint BASE_MATERIAL BEHAVIORS
BEHAVIOR_MECHANIC_AUDIO BEHAVIOR_MECHANIC_DAMAGE BEHAVIOR_MECHANIC_DRAW BEHAVIOR_MECHANIC_EFFECTS
BEHAVIOR_MECHANIC_ENGINE BEHAVIOR_MECHANIC_INPUT BEHAVIOR_MECHANIC_RESET BEHAVIOR_MECHANIC_RIGIDBODY
BEHAVIOR_MECHANIC_SUSPENSION BEHAVIOR_ORDER BIGPath BONDMOVE_DIE_CLEAR BONDMOVE_RESTART_CLEAR BRAKES
BRAKE_LOCK BURemindTime BackTime BackupCallTimer BankName BankName_auxRAM BankName_mainRAM
BarrierExemptions Barriers BaseMaterial BaseOpenWorldHeat BaselineUnlocks BinIndex BlackBloomCurve
BlackBloomIntensity BlowbyInterval BmMultiply BodyDive BodyRoll BodySquat Boss BossRace BossRaces
BossReputation Bounty BoxinDuration BoxinTightness BrMultiply BustSpeed BustedLives
CAMERAACTION_CHANGE CAMERAACTION_DEBUG CAMERAACTION_ENABLE_ICE CAMERAACTION_LOOKBACK
CAMERAACTION_PULLBACK CAMERA_AVOIDABLE CAMERA_NOISE CAN_SIMPLIFY CG CHAIN_NEXT_MISSION
CHAIN_NEXT_MISSION_NAME CHECK_PLAYER_BEHIND_TRAFFIC CLASS CLUTCH_SLIP COLLISION_BOX_PAD
CONTROLLER_CURVE COST_TO_STATE CSIPath CSIS_EFFECT CTSFor911 CacheDisplayCoords CamberFront
CamberRear CameraInfo_Bumper CameraInfo_Close CameraInfo_Drift CameraInfo_Far CameraInfo_Hood
CameraInfo_Pursuit CameraInfo_SuperFar CameraModelMarker CanInterupt CannedPath CarID CarPartName
CarType CarTypeLowMem CashReward CashRewards CashValue CatchUp CatchUpDerivative CatchUpIntegral
CatchUpOverride CatchUpSkill CatchUpSpread CellChallengeRace ChallengeSeriesRace ChanceOfRain
ChargeTime Checkpoint CheckpointsVisible Children Clarity CollapseAggression CollapseInnerRadius
CollapseOuterRadius CollapseSpeed CollectionName CollectorsEditionRace CollisionMinClosingVelSq
Color1 Color2 Color3 Color4 Colour1 ColourBloomCurve ColourBloomIntensity ColourBloomTint ConstantBm
ConstantBr CopDensity CopFormations CopSpawnPoints CopSpawnType CopsInRace Cost
CostToStateForDestroying CrashSlowdownPct CullingRange DDayRace DEBUGACTION_DROPCAR
DEBUGACTION_LOOK_DOWN DEBUGACTION_LOOK_D_DOWN DEBUGACTION_LOOK_D_LEFT DEBUGACTION_LOOK_D_RIGHT
DEBUGACTION_LOOK_D_UP DEBUGACTION_LOOK_LEFT DEBUGACTION_LOOK_RIGHT DEBUGACTION_LOOK_UP
DEBUGACTION_MOVE_BACK DEBUGACTION_MOVE_DOWN DEBUGACTION_MOVE_D_BACK DEBUGACTION_MOVE_D_FORWARD
DEBUGACTION_MOVE_D_LEFT DEBUGACTION_MOVE_D_RIGHT DEBUGACTION_MOVE_FORWARD DEBUGACTION_MOVE_LEFT
DEBUGACTION_MOVE_RIGHT DEBUGACTION_MOVE_UP DEBUGACTION_SIMSTEP DEBUGACTION_SUPER_TURBO
DEBUGACTION_TOGGLEAI DEBUGACTION_TOGGLECARCOLOUR DEBUGACTION_TOGGLEDEMOCAMERAS
DEBUGACTION_TOGGLESIMSTEP DEBUGACTION_TURBO DEBUG_COLOUR DECEL_AEMSMix_L_RPM DECEL_AEMSMix_S_RPM
DECEL_AEMSVol DECEL_GINSUMix_L_RPM DECEL_GINSUMix_S_RPM DEFAULT_COL_BOX DESCRIPTION DETACH_FORCE
DIFFERENTIAL DRAG DRAG_ANGULAR DRAG_COEFFICIENT DRIVE_GRIP DRIVE_SPEED DROPOUT DYNAMIC_GRIP
DZ_BOTTOM DZ_FRONT DZ_LEFT DZ_LFRONT DZ_LREAR DZ_REAR DZ_RFRONT DZ_RIGHT DZ_RREAR DZ_TOP
DamageEffect DeadAir DeathEffect DebugJumpPoint DecelDeltaRPMThreshold DecelPitchOffset DefPlay
DefaultPresetRide DelayTime DepFollow Desaturation DestroyCopBonusTime DetachmentID DetailMapCurve
DetailMapIntensity DifficultyLevel Dimensions Directional DoCountdown DoNotDropout DoPhotofinish
Down_Disengage_Fall_RPM Down_Disengage_Fall_T Down_Engaging_Fall_RPM Down_Engaging_Fall_T
Down_Engaging_Rise_RPM Down_Engaging_Rise_T Down_Reattach_Scale Down_Shift_Sound_Delay
Down_Vol_Shift Drag EBRAKE ENGINE_BRAKING EVTPath EliminateUnnecessaryRandomness EmitterQuadratic
Emitters EndU EndV EnforceDeadAir EngType EngineBlownEffect EngineRev EngineRevAngle EngineRevSpeed
EngineVibrationFreq EngineVibrationMax EngineVibrationMin EntryActivity EntryCellCallID
EvadeSuccessHeatAdjust EventID EventIconType EventSequencer EventWinHeatAdjust EvtSys
ExcludedCharacters ExplosionEffect ExtraPitch ExtraRearTireOffset FECompressions FESpeech FFB_ROLL
FFB_SKID FFB_SLIP FINAL_GEAR FLOW_RATE FLYWHEEL_MASS FORCE FOV FRONTENDACTION_ACCEPT
FRONTENDACTION_BUTTON0 FRONTENDACTION_BUTTON1 FRONTENDACTION_BUTTON2 FRONTENDACTION_BUTTON3
FRONTENDACTION_BUTTON4 FRONTENDACTION_BUTTON5 FRONTENDACTION_CANCEL FRONTENDACTION_CANCEL_ALT
FRONTENDACTION_DOWN FRONTENDACTION_LEFT FRONTENDACTION_LTRIGGER FRONTENDACTION_RDOWN
FRONTENDACTION_RIGHT FRONTENDACTION_RLEFT FRONTENDACTION_RRIGHT FRONTENDACTION_RTRIGGER
FRONTENDACTION_RUP FRONTENDACTION_START FRONTENDACTION_UP FRONTSIDE_REACTION FRONT_AXLE
FRONT_REACTION FRONT_WEIGHT_BIAS FarClip Filename_GinsuAccel Filename_GinsuDecel FilterBlocks
FilterModePassAll FinishCamera FireOnExit FlareSpacing FlipTimeForCommentary FogEnable FogG
ForceHeatLevel ForcePreload ForceStartPosition ForceTrafficDensity FreeRoamOnly FrontCamber
FullEngagementCopCount FullEngagementRadius GAMEACTION_BACK GAMEACTION_BRAKE GAMEACTION_DEBUGHUMAN1
GAMEACTION_DEBUGHUMAN2 GAMEACTION_DEBUGHUMAN3 GAMEACTION_DEBUGHUMAN4 GAMEACTION_FORWARD
GAMEACTION_GAMEBREAKER GAMEACTION_GAS GAMEACTION_HANDBRAKE GAMEACTION_JUMP GAMEACTION_NOS
GAMEACTION_RESET GAMEACTION_SHIFTDOWN GAMEACTION_SHIFTUP GAMEACTION_STEERLEFT GAMEACTION_STEERRIGHT
GAMEACTION_TURNLEFT GAMEACTION_TURNRIGHT GEAR_EFFICIENCY GEAR_RATIO GINSUAccelVol GINSUMix_L_RPM
GINSUMix_S_RPM GINSU_DECEL_FADE_IN GINSU_DECEL_FADE_OUT GINSU_Decel_MaxRPM GINSU_Decel_MinRPM
GINSU_LowPassCutoff GRAVITY GRIP_SCALE GROUND_ELASTICITY GROUND_FRICTION GROUND_MOMENT_SCALE
GateActivity GinsuDecelVol Ginsu_ACL_Neg_L_RPM Ginsu_ACL_Neg_S_RPM GoalAddPrevBest GoalEasy GoalHard
Gravity GravityDelta GravityStart HEIGHT HENCH_SLOTS_AVAIL HIGH_BOOST HIT_POINTS HP_THRESHOLD
HUDACTION_ENGAGE_EVENT HUDACTION_NEXTSONG HUDACTION_PAD_DOWN HUDACTION_PAD_LEFT HUDACTION_PAD_RIGHT
HUDACTION_PAUSEREQUEST HUDACTION_SKIPNIS HandlingRating HangTimeForCommentary HeavySupportOptions
HeightStart HeliFuelTime HiddenZoneTimeMultiplier HighIntensityMark HornType ICEACTION_ALT_1
ICEACTION_BUBBLE_BACK ICEACTION_BUBBLE_DOWN ICEACTION_BUBBLE_FORTH ICEACTION_BUBBLE_IN
ICEACTION_BUBBLE_LEFT ICEACTION_BUBBLE_OUT ICEACTION_BUBBLE_RIGHT ICEACTION_BUBBLE_SPIN_LEFT
ICEACTION_BUBBLE_SPIN_RIGHT ICEACTION_BUBBLE_TILT_DOWN ICEACTION_BUBBLE_TILT_UP ICEACTION_BUBBLE_UP
ICEACTION_CANCEL ICEACTION_CLIP_IN ICEACTION_CLIP_OUT ICEACTION_COPY ICEACTION_CUT ICEACTION_DOWN
ICEACTION_DUTCH_LEFT ICEACTION_DUTCH_RIGHT ICEACTION_FAST_LEFT ICEACTION_FAST_RIGHT
ICEACTION_GRAB_LEFT ICEACTION_GRAB_RIGHT ICEACTION_HELP ICEACTION_HIDE ICEACTION_INSERT
ICEACTION_LEFT ICEACTION_LETTERBOX_IN ICEACTION_LETTERBOX_OUT ICEACTION_PASTE ICEACTION_PLAY
ICEACTION_RIGHT ICEACTION_SCREENSHOT ICEACTION_SCREENSHOT_STREAM ICEACTION_SELECT
ICEACTION_SHAKE_FRQ_DEC ICEACTION_SHAKE_FRQ_INC ICEACTION_SHAKE_MAG_DEC ICEACTION_SHAKE_MAG_INC
ICEACTION_SHUTTLE_DOWN ICEACTION_SHUTTLE_LEFT ICEACTION_SHUTTLE_RIGHT ICEACTION_SHUTTLE_SPEED_DEC
ICEACTION_SHUTTLE_SPEED_INC ICEACTION_SHUTTLE_UP ICEACTION_SIMSPEED_DEC ICEACTION_SIMSPEED_INC
ICEACTION_UNDO ICEACTION_UP ICEACTION_ZOOM_IN ICEACTION_ZOOM_OUT IDLE IDXPath
IMMOBILE_OBJECT_COLLISIONS INSTANCE_COLLISIONS_3D IconModelBounceAmp IconModelBounceRate
IconModelFloatHeight IconModelName IconModelScale IconModelSpinRate InGameSpeech InScatterMulitply
InheritVelocity InitDelay InitialAngleRange InitialPlayerSpeed InitialSpeed InitiallyUnlocked
IntensityRanges InternalRaceIndex Interop::SafeHouseCarSlots Interruptable Interval IntroCameraTrack
IntroMessageID IntroMovie IntroNIS IsBoss IsCustomizable IsEpicPursuitRace IsLoopingRace
IsMarkerRace IsOneShot IsPositioned IsSkinned IsWooshable JumpRaces KILL_OFF_SCREEN KeyPositions
KitWheelOffsetFront KitWheelOffsetRear KnockoutTime KnockoutsPerLap LAG LATERAL_GRIP LATOFFSET
LFO_RPM_Amp LFO_RPM_Decay_Time LFO_RPM_Freq LFO_Vol_Amp LFO_Vol_Decay_Time LFO_Vol_Freq
LIGHT_COPBLUE LIGHT_COPRED LIGHT_COPWHITE LINEAR_TRACK LOW_BOOST LeaderSupportOptions Leak_Rate
LengthDelta LengthStart LicensedMusic Life LifeVariance Lifetime911 Locales LocalizationTag MASS
MATERIAL_STRENGTH MAXIMUM_AI_SPEED MAX_FRAGMENTS MAX_NEWTONS MAX_RPM MAX_SMACKABLES MAX_SPEED_MPS
MAX_TRAFFIC MAX_TRAFFIC_SPAWN_DISTANCE MIN_TRAFFIC_SPAWN_DISTANCE MODEL MOMENT MasterCheckpoint
Master_Vol MaxCarRep MaxCopsCollapsing MaxHeat MaxHeatLevel MaxOpenWorldHeat MaxPlayback
MaxPlayerSpeed MaxPursuitRep MaxRPM MaxRangeFor180 MaxRangeForPrimaryBranch MaxSize MaxTimeFor180
MaxTireSteer Maxscale MaybeV8 MedalBonusBronze MedalBonusGold MedalBonusSilver
MeterDeadZoneBustedDistance MeterDeadZoneEvadeDist MilestoneBiggerIsBetter MilestoneChallenge
MilestoneCompleteHeatAdjust MilestoneName MilestoneType MinContigFramesFor180
MinHavocForSuspectBehavior MinHealthForCommentary MinHeat MinHeightAirborne MinIntensityCopSmash
MinIntensitySideswipe MinIntensityTrafficSmash MinPlayerSpeed MinPursuitDurationForBailouts MinRPM
MinScale MinSize MinSpeedConsideredStopped MinTimeConsideredStopped MiniMapItem MinimumAIPerformance
MinimumSupportDelay MissShiftEffect MotionInherit MotionInheritVariance MotionLive
NATURAL_ANGULAR_DAMPING NGEmitter NISShell NOSEffect NOS_CAPACITY NOS_DISENGAGE NO_CAR_EFFECT
NO_GROUND_COLLISIONS NO_OBJ_COLLISIONS NO_WORLD_COLLISIONS NUM_CHECKPOINTS NUM_PED_TYPES_GC
NUM_PED_TYPES_PS2 NUM_PED_TYPES_XBOX Name NameId NeverInQuickRace NewBodyKit NewDecal NewHood
NewNumbers NewPaint NewRim NewRimPaint NewRoofScoop NewSpoiler NewVinyl NewWindowTint NoKillAtAlpha
NoLOSCommentaryTime NoPostRaceScreen NumCiviHitsFor911 NumCopsToTriggerBackup NumLaps NumParticles
NumParticlesVariance NumPatrolCars NumRacesRequired OBJ_ELASTICITY OBJ_FRICTION OBJ_MOMENT_SCALE
OPTIMAL_SHIFT OffCycle OffCycleVariance OnBottomOut OnBottomScrape OnCycle OnCycleVariance
OnDetached OnHitGround OnHitObject OnHitWorld OnScrapeGround OnScrapeObject OnScrapeWorld
OnScreenOnly OnTireBlow OneShot OpenWorldSpeedTrap Opponents OutcomeFailSpeed OutcomeTrackTime
OutroCameraTrack OutroMessageID OutroMovie OutroNIS OutroNISMarker OvertimePenaltyPerSec PED_OBJECTS
PED_SPAWN_RADIUS PFMapping PITCH_ALIGN_SCALE PITCH_ANG PITCH_SLOW_DOWN_RATE PITCH_STOP_SPEED PSI Pan
ParticleEffect PathEvent Pauseable Persistent PhysicsRPM_Map PlayerCarPerformance PlayerCarType
PlayerCollisions PlayerCollisionsDefault PlayerSmashSpeedRange PlayerUsable Position
PostRaceActivity PostRaceScreenTexture Power PresetRide Priority PriorityWeight ProgressionLevel
PursuitDurationHighIntensity PursuitInactivityTimer PursuitInitDelay PursuitLevel PursuitRace
QuickRaceNIS QuickRaceUnlocked RACE_SCORING RBOutcomeTimer RBPostOutcomeResetTime
REACTION_ACTIVATE_EDIT REACTION_APPLY_TO_NEXT REACTION_AUTOFIT REACTION_EDIT_NODE REACTION_LOOKDOWN
REACTION_LOOKLEFT REACTION_LOOKRIGHT REACTION_LOOKUP REACTION_MOVE_BACK REACTION_MOVE_DOWN
REACTION_MOVE_FORWARD REACTION_MOVE_LEFT REACTION_MOVE_RIGHT REACTION_MOVE_UP REACTION_NEXTLANE
REACTION_PICK REACTION_PREVIOUSLANE REACTION_SELECTNEXT REACTION_TURBO REARSIDE_REACTION
REAR_REACTION RECHARGE_MAX RECHARGE_MAX_SPEED RECHARGE_MIN RECHARGE_MIN_SPEED RED_LINE RENDER_MOTION
RESPAWN_TIME RIDE_HEIGHT RIM_SIZE ROLLING_RESISTANCE ROLL_ALIGN_SCALE ROLL_ANG ROLL_CENTER
ROLL_SLOW_DOWN_RATE ROLL_SPEED_MIN ROLL_START_SPEED RaceLength RaceList RaceTriggers RacerName
RadioChirp Radius RandomOpponent RandomRotationDirection RandomSpawnTriggers RangeForSpotterBranch
RankPlayersByDistance RankPlayersByPoints RapSheetRanks RecallList RedundancyCheckIsGlobal
ReflectionOffset Region RelativeAngle RenderNoise RepPointsForDestroying RepPointsPerMinute
Reputation ReputationRequired RequiredBounty RequiredChallenges RequiredRacesWon ResetTime
ResetWhenPursuitStarts ResetsPlayer RespawnMarker RestartActivity RewardMarkerType RewardsForWinner
RideHeight RingTime RivalBestTime RoadList RoadNoise RollingBlockDuration RollingBlockTightness
RollingStart Rotation RotationVariance SAME_ALIGN_SCALE SCALE_STEERING SECTION_WIDTH SELECTABLE
SHIFT_SPEED SHOCK_BLOWOUT SHOCK_DIGRESSION SHOCK_EXT_STIFFNESS SHOCK_FORCE SHOCK_STIFFNESS
SHOCK_TIME SHOCK_VALVING SLEEP_VELOCITY SMSCellChallenge SMSRivalChallenge SPEED_LIMITER
SPLITMISSION_CARRYDAMAGE SPLITMISSION_NEXTHALF SPLITMISSION_PREVHALF SPOOL SPOOL_TIME_DOWN
SPOOL_TIME_UP SPRING_PROGRESSION SPRING_STIFFNESS STATIC_GRIP STEERING STICK STIFFNESS
STITCH_LEVEL_0 STITCH_LEVEL_1 STITCH_LEVEL_2 STITCH_LEVEL_3 STRAFE_SCALEX STRAFE_SCALEY
SUPPRESS_DIST SWAYBAR_STIFFNESS ScaleEscalationPerBucket ScaleOpenWorldHeat ScriptedCopsInRace
SearchModeCityMPH SearchModeHeliSpawnChance SearchModeHwyMPH SearchModeRoadblockChance
SearchModeRoadblockRadius SharedCheckpoints ShiftAngle ShiftSND ShiftSpeed ShortcutMaxChance
ShortcutMinChance Shortcuts SimControls SimSubSystems SimTasks SimplePhysics SirenInitMinPeriod
SirenInitVariation SirenMaxScreamTime SirenMaxYelpTime SirenScreamPeriod SirenWailPeriod Size SkidFX
SkillLevel SlipFX SongName SpawnPoint SpawnTime SpeechDropoffRamp SpeechID Speed SpeedDiffForBlowby
SpeedHighway SpeedReactionTime SpeedStreet SpeedThreshFlyFlipIntensity SpeedTrapCamera SpeedTrapList
SpeedTrapTrigger SpeedTrapsRequired SpeedVariance Spin SpreadAngle SpreadAsDisc StaggerFormationTime
StartDelay StartDelayRandomVariance StartPercent StartTime StartU StartV SteerSpeed StreamSweetner
SuspectOutrunRange SweetBank TENSOR_SCALE TETHER_WEIGHT TILTING TOD TORQUE TORQUE_BOOST
TORQUE_CONVERTER TORQUE_SPLIT TRACK_WIDTH TRAFFIC_LANE_CHANGES TRAFFIC_SPEED TRAFFIC_TYPES TRAVEL
TURN_BOOST_SPEED TargetActivities TargetActivity TargetBronze TargetGold TargetMarker TargetSilver
Template Texture TextureAnimation ThreshholdSpeed ThreshholdValue TimeBetweenCopSpawn
TimeBetweenFirstFourSpawn TimeBetweenHeliActive TimeBonus TimeConsideredLostNoLOS TimeInactiveFor911
TimeLimit TimePerHeatLevel TimeToHideInZone TimeWaitForSpotterReply TireDriveEffects TireOffsets
TireSkidWidth TireSkidWidthKitScale TireSlideEffects TireSlipEffects TokenValue TopSpeedMultiplier
TrafficCharacter TrafficEngType TrafficLevel TrafficPattern Trailer Tranny TruckSndFX TurboSND
UnlockRaces UnlockedAt Up_DisengageFall Up_DisengageFall_Curve Up_Engage Up_Engage_Curve
Up_Engaging_Attack_T Up_Engaging_Attack_Vol Up_Shift_Sound_Delay Up_Vol_Shift UpgradeLevel
UpgradePartID UpgradePartName UpgradeType UseWorldHeat VACUUM VEHICLE_SLOTS_AVAIL
VOIPACTION_PUSHTOTALK Vehicles VelocityDelta VelocityInherit VelocityStart VerbalType VisualCullDist
VolSlot Vol_Blowoff1 Vol_Blowoff2 Vol_ShiftSweets Vol_Spool Vol_Sputters VolumeCenter VolumeExtent
Volumes WALL_ELASTICITY WALL_FRICTION WHEEL_BASE WORLD_FRICTION WORLD_MOMENT_SCALE WORLD_TYPE
WarningLevel WheelEffectFrequency WheelEffectIntensity WheelHopScale WheelSpokeCount
WheelSurfaceEffect WheelWell Width WooshType WorldRaces XenonEffect YAW_BOOST_LIMIT YAW_CONTROL
YAW_DAMP YAW_LIMIT_FRONT YAW_LIMIT_REAR YAW_SPEED YAW_STRENGTH_FRONT YAW_STRENGTH_REAR ZBias
ZoneList ZoneType _testvalue a3_base a3_top a4_base a4_top accel_from_idle_00 accel_from_idle_auto
accel_from_idle_race acceltrans actionscript ai_totalled aivehicle amount assault aud_moment_strm
aud_stitch_loop audioimpact audioscrape audiosystem backup barrel_2_car barrel_2_world barrierbreak
bigsmack_2_car bike_2_car bike_2_world bl10 bl11 bl12 bl13 bl14 bl15 bl2 bl3 bl4 bl5 bl6 bl7 bl8 bl9
blindedinchains bloodandthunder blownengine blue boat_2_car boat_2_world brakelight_inner
brakelight_traffic_inner brakes brakes_current brakes_package brakes_upgrades brokensword
bsidewinsagain bytecode cache_OnCreate cache_SysInit cam_angle cam_anim_speed cam_blur cam_damping
cam_fov cam_lookat_x cam_lookat_y cam_lookat_z cam_orbit_horizontal cam_orbit_radius
cam_orbit_vertical cam_periods cam_roll_angle cam_user_rotate camera_flash camerainfo car2car
car_2_car car_2_car_front car_2_car_side car_2_ground car_2_ground_bottomout car_2_ground_roll
car_2_smackable car_2_wall car_2_wall_front car_2_wall_side car_to_wall_crash cardboard_2_car
cardboard_2_world carreragt_top carriercars castrolgt catseye_blue catseye_orange catseye_red
caymans_base caymans_top ce_997s ce_c6r ce_camaro ce_corvette ce_elise ce_gt2 ce_gtrstreet ce_sl500
ce_supra chainlink_brk chassis chassis_current chassis_package chassis_upgrades chlkfence_2_car
chlkfence_2_world chopper chopperspecs clio_base clio_top clk500_top cobaltss_top coin_spray
collision collisionreactions colour cone_2_car cone_2_world contrail controller copc6 copcam
copcompact coplightblue_inner coplightbrightblue coplightbrightred coplightorange coplightred_inner
coplightwhite_inner copnonos coppit cops corvette_top crate_2_car crate_2_world cscopmidsize
cscopsuv cssuv cts_base cts_top damage damageMultiplier damagespecs db9_base db9_top decadence
default default_car disengagetrigger distance doTest donut_2_car donut_2_world doyathang
dumpster_2_car dumpster_2_world ecar eclipsegt_base eclipsegt_top effects elise_base elise_top
emitterdata emittergroup emitteruv emsprk_line1 emsprk_line2 engagetrigger engine engine_breakdown
engine_current engine_package engine_upgrades engineaudio english evadetimeout expiry explosion
explosionForceLimit extreme_uves fallOffUnit feCarPartInfo feCarPartName fecooling feedmyaddiction
feelslikeitshould firedup flare_texture fly_in_camera foglight_inner forceMultiplier fordgt_top
formations fragment french frontLOSdistance frontend fuelcell_effect fuelcell_emitter fxsprk_line
gallardo_top game_camera gameplay gameplayvault german glass_2_car glass_2_world glass_med_brk
golfcart_2_car golfcart_2_world graph green green_flare gti_top gto_base gto_top hand_flare_red
handler_owner handofblood headlight_inner headlight_outer heattable heattrigger heavygravity
heliLOSdistance henchchopper hit_and_run huge_object_2_car huge_object_2_world hydrant_2_car
hydrant_2_world iamroc immobile immobileheavygravity imperzawrx imprezawrx_top induction
induction_current induction_package induction_upgrades infractions inmyhood interrupt is300_base
is300_top italian jacknife japanese jump_cam jumpcam junkman junkman_current lamp_lrg_2_car
lamp_lrg_2_world lamppost_inner lamppost_outer lancerevo8_top lawn_chr_2_car lawn_chr_2_world
layer1_specular_colour layer1_tile_xy layer2_specular_colour layer2_tile_xy layoutpos length
letsmove light_flares_cg log_2_car log_2_world magnitude manufacturer mast_2_car mast_2_world
message_id milestonetypes monaro_top mostwanted mtl_bar_2_car mtl_bar_2_world mtl_bus_shltr_2_car
mtl_bus_shltr_2_world mtl_fence_2_car mtl_fence_2_world mtl_gate_2_car mtl_gate_2_world mtlbox_2_car
mtlbox_2_world murcielago_top muscle_fast muscle_med muscle_v_fast music mustanggt_base
mustanggt_top mwlicensedmusic mwnissfxstreams ninethou nissfxstreams nitrouslevel no_trigger normal
nos nos_base nos_current nos_package nos_radialblur nos_top nos_upgrades ocean off_road
onegoodreason overlap p_brakes_1 p_brakes_2 p_brakes_3 p_brakes_4 p_engine_1 p_engine_2 p_engine_3
p_engine_4 p_induction_1 p_induction_2 p_induction_3 p_induction_4 p_nitrous_1 p_nitrous_2
p_nitrous_3 p_nitrous_4 p_suspension_1 p_suspension_2 p_suspension_3 p_suspension_4 p_tires_1
p_tires_2 p_tires_3 p_tires_4 p_transmission_1 p_transmission_2 p_transmission_3 p_transmission_4
panel_2_car panel_2_world param parentstate patioset_2_car payphone_2_car payphone_2_world
ph_booth_brk pop_mach_2_car pop_mach_2_world portpotty_2_car portpotty_2_world post_light_2_car
post_light_2_world presetride priority prison_jump prkg_mtr_2_car prkg_mtr_2_world punto_base
punto_top purs_break_end_fx purs_break_end_music purs_break_start_fx purs_break_start_music
pursuit_breaker_moments pursuitescalation pursuitlevels pursuitsupport pvehicle qchopper racefinish
racefinishReverse racestart racestartReverse racetable radialblur_scale radialblur_uvoffset
rake_2_car rake_2_world rearLOSdistance reckless_driving red reflection_amount region reqLOS
resisting_arrest reverselight_inner rigidbodyspecs roadblockcopghost roadblockcopmidsize
roadblockcopsportghost roadblockcopsporthench roadblockgto roadblockgtoghost roadblockhelichance
roadblockprobability roadblockspikechance roadblocksuvheavy roadblocksuvlight rx7_top rx8_base
rx8_top sandbrd_2_car sandbrd_2_world sawhorse_2_car sawhorse_2_world scrape scriptname seats_2_car
seats_2_world sender setsgoup shapeshifter shelt_frag_2_world shiftpattern shrub_2_car shrub_2_world
shrub_break sign_2_car sign_2_world simsurface skinnyman sl500_base sl500_top slr_top smackable
smackable_2_car smackable_2_ground smackable_2_world smackables soft_furn_2_car soft_furn_2_world
spanish speech speechtune speeding sport_fast sport_v_fast spot_cop_camera start_sequencer starter
stateref stitch stream strmpriority sun_flare sunset superchrg_cobra superchrg_gen superchrg_mbz
superchrg_must support01 support02 support03 support04 support05 support06 support07 support08
support09 support10 supportrace_01 supportrace_02 supportrace_03 supportrace_04 supportrace_05
supportrace_06 supportrace_07 supportrace_08 supportrace_09 supportrace_10 supportracetable
supporttable supra_top suv_fast suv_v_fast suvlight suvs system taoofthemachine target templateref
thunder_close thunder_distant tilted tire_2_car tire_2_world tires tires_current tires_package
tires_upgrades tollbooth_brk totalled traffic_light traffic_light_red trafficpattern trafstreet
traftruck trafvan trail trail2 trail3 transitionlist transmission transmission_current
transmission_package transmission_upgrades trashcan_2_car trashcan_2_world tree_2_car tree_break
triggerThreshold tt_base tt_top tuner_fast tuner_med tuner_v_fast turbo_lrg_00 turbo_lrg_01
turbo_lrg_02 turbo_med_00 turbo_med_01 turbo_med_02 turbo_semi turbo_sml1_00 turbo_sml1_01
turbo_sml1_02 turbo_sml2_00 turbo_sml2_01 turbo_sml2_02 turbosfx umbrella_2_car umbrella_2_world
uves uves_extreme uves_pulse uves_pulse_length uves_pulse_trigger uves_radialblur uves_transition
vend_mach_2_car vend_mach_2_world viper_top visuallook visuallookeffect visuallooktransition
visualrgbtweaker wd_fence_2_car wd_furn_brk wd_roadblk_brk wecontrol window wood_2_car wood_2_world
wood_frag_lrg_2_car wood_frag_lrg_2_world wood_frag_med_2_car wood_frag_med_2_world
wood_frag_sm_2_car wood_frag_sm_2_world wood_furn_2_car wood_furn_2_world wood_support_2_car
wood_support_2_world world wtr_drum_2_car wtr_drum_brk wtr_drum_emp_2_car wtr_drum_emp_2_world
youllbeundermywheels youmustfollow zContrail zSprite
"""

# Common vehicle & collection names (supplementing StrE + CollectionName auto-discovery)
KNOWN_NODE_NAMES = [
    "default",
    "midday",
    "sunset",
    "uves",
    "copcam",
    "bumper",
    "hood",
    "close",
    "far",
    "superfar",
    "drift",
    "drag",
    "bmwm3gtre46",
    "bmwm3gtr",
    "gallardo",
    "carreragt",
    "corvette",
    "corvettec6r",
    "fordgt",
    "viper",
    "murcielago",
    "rx7",
    "rx8",
    "rx8speedt",
    "supra",
    "imprezawrx",
    "lancerevo8",
    "gto",
    "mustanggt",
    "camaro",
    "cobaltss",
    "punto",
    "gti",
    "is300",
    "a3",
    "a4",
    "tt",
    "clk500",
    "sl500",
    "sl65",
    "slr",
    "db9",
    "elise",
    "caymans",
    "911turbo",
    "911gt2",
    "997s",
    "cts",
    "monaro",
    "eclipsegt",
    "clio",
    "copmidsize",
    "copghost",
    "copsuv",
    "copsuvl",
    "copcross",
    "copsport",
    "copsportghost",
    "copsporthench",
    "copheli",
    "pursuitbreaker",
    "heat_01",
    "heat_02",
    "heat_03",
    "heat_04",
    "heat_05",
    "heat_06",
    "heat_07",
    "heat_08",
    "heat_09",
    "heat_10",
    "race_01",
    "race_02",
    "race_03",
    "race_04",
    "race_05",
    "race_06",
    "race_07",
    "race_08",
    "race_09",
    "race_10",
]


def build_default_name_map(extra_names: Optional[Sequence[str]] = None) -> Dict[int, str]:
    """Build a VLT32 hash -> name lookup dictionary from built-in CC0 names."""
    hmap: Dict[int, str] = dict(VLT_CLASS_HASHES)
    for token in _CC0_VLT_NAMES.split():
        hmap.setdefault(vlt32_hash(token), token)
    for token in KNOWN_NODE_NAMES:
        hmap.setdefault(vlt32_hash(token), token)
    if extra_names:
        for token in extra_names:
            t = token.strip()
            if t:
                hmap[vlt32_hash(t)] = t
    return hmap


# ---------------------------------------------------------------------------
# Known Composite Struct Layouts (verified from CC0 dbalatoni13/nfsmw headers)
# ---------------------------------------------------------------------------
# Maps AttribSys type name -> list of (member_name, member_type_name, byte_offset)

STRUCT_SCHEMAS: Dict[str, List[Tuple[str, str, int]]] = {
    "AxlePair": [
        ("Front", "EA::Reflection::Float", 0),
        ("Rear", "EA::Reflection::Float", 4),
    ],
    "CarBodyMotion": [
        ("DegPerG", "EA::Reflection::Float", 0),
        ("MaxGs", "EA::Reflection::Float", 4),
        ("DegPerSec", "EA::Reflection::Float", 8),
    ],
    "DamageScaleRecord": [
        ("VisualScale", "EA::Reflection::Float", 0),
        ("HitPointScale", "EA::Reflection::Float", 4),
    ],
    "RoadNoiseRecord": [
        ("Frequency", "EA::Reflection::Float", 0),
        ("Amplitude", "EA::Reflection::Float", 4),
        ("MinSpeed", "EA::Reflection::Float", 8),
        ("MaxSpeed", "EA::Reflection::Float", 12),
    ],
    "JunkmanMod": [
        ("ClassKey", "Attrib::Key", 0),
        ("DefinitionKey", "Attrib::Key", 4),
        ("Scale", "EA::Reflection::Float", 8),
    ],
    "TrafficPatternRecord": [
        ("Vehicle", "Attrib::RefSpec", 0),
        ("Rate", "EA::Reflection::Float", 12),
        ("MaxInstances", "EA::Reflection::UInt32", 16),
        ("Percent", "EA::Reflection::UInt32", 20),
    ],
    "CopCountRecord": [
        ("CopType", "Attrib::StringKey", 0),
        ("Count", "EA::Reflection::UInt32", 16),
        ("Chance", "EA::Reflection::UInt32", 20),
    ],
    "CopFormationRecord": [
        ("Formation", "EA::Reflection::UInt32", 0),
        ("Duration", "EA::Reflection::Float", 4),
        ("Frequency", "EA::Reflection::Float", 8),
    ],
    "AirSupport": [
        ("HeliStrategy", "EA::Reflection::UInt32", 0),
        ("Chance", "EA::Reflection::UInt32", 4),
        ("Duration", "EA::Reflection::Float", 8),
    ],
    "HeavySupport": [
        ("HeavyStrategy", "EA::Reflection::UInt32", 0),
        ("Chance", "EA::Reflection::UInt32", 4),
        ("Duration", "EA::Reflection::Float", 8),
        ("ChanceBigSUV", "EA::Reflection::UInt32", 12),
    ],
    "LeaderSupport": [
        ("LeaderStrategy", "EA::Reflection::UInt32", 0),
        ("Chance", "EA::Reflection::UInt32", 4),
        ("Duration", "EA::Reflection::Float", 8),
        ("PriorityChance", "EA::Reflection::UInt32", 12),
        ("PriorityTime", "EA::Reflection::Float", 16),
    ],
    "AICollisionReactionRecord": [
        ("Goal", "EA::Reflection::UInt32", 0),
        ("Reaction", "Attrib::RefSpec", 4),
    ],
    "EffectLinkageRecord": [
        ("mSurface", "Attrib::RefSpec", 0),
        ("mEffect", "Attrib::RefSpec", 12),
        ("mMinSpeed", "EA::Reflection::Float", 24),
        ("mMaxSpeed", "EA::Reflection::Float", 28),
    ],
    "TireEffectRecord": [
        ("mEmitter", "Attrib::RefSpec", 0),
        ("mMinSpeed", "EA::Reflection::Float", 12),
        ("mMaxSpeed", "EA::Reflection::Float", 16),
    ],
    "CollisionReactionRecord": [
        ("Elasticity", "EA::Reflection::Float", 0),
        ("RollHeight", "EA::Reflection::Float", 4),
        ("WeightBias", "EA::Reflection::Float", 8),
        ("MassScale", "EA::Reflection::Float", 12),
        ("StunSpeed", "EA::Reflection::Float", 16),
        ("StunTime", "EA::Reflection::Float", 20),
    ],
    "FECarPartInfo": [
        ("Level", "EA::Reflection::Int32", 0),
        ("Rep", "EA::Reflection::Float", 4),
        ("Cost", "EA::Reflection::Float", 8),
    ],
}

PRIMITIVE_STRUCT_FMTS: Dict[str, Tuple[str, int]] = {
    "EA::Reflection::Float": ("<f", 4),
    "EA::Reflection::Double": ("<d", 8),
    "EA::Reflection::Int32": ("<i", 4),
    "EA::Reflection::UInt32": ("<I", 4),
    "EA::Reflection::Int16": ("<h", 2),
    "EA::Reflection::UInt16": ("<H", 2),
    "EA::Reflection::Int8": ("<b", 1),
    "EA::Reflection::UInt8": ("<B", 1),
    "EA::Reflection::Char": ("<b", 1),
    "EA::Reflection::Int64": ("<q", 8),
    "EA::Reflection::UInt64": ("<Q", 8),
}

VECTOR_TYPES: Dict[str, int] = {
    "Attrib::Types::Vector2": 2,
    "Attrib::Types::Vector3": 3,
    "Attrib::Types::Vector4": 4,
    "Attrib::Types::FloatColour": 4,
    "Attrib::Types::Quaternion": 4,
}

# Built-in primitive type hashes so collections in FE_ATTRIB / GAMEPLAY / synthetic fixtures
# decode cleanly even when a standalone vault omits DatabaseLoadData.
BUILTIN_TYPES: Dict[int, Tuple[str, int]] = {
    vlt32_hash(name): (name, sz)
    for name, sz in [
        ("EA::Reflection::Text", 4),
        ("EA::Reflection::Double", 8),
        ("EA::Reflection::UInt16", 2),
        ("EA::Reflection::UInt64", 8),
        ("EA::Reflection::Bool", 4),
        ("EA::Reflection::Char", 1),
        ("EA::Reflection::Int32", 4),
        ("EA::Reflection::Float", 4),
        ("EA::Reflection::Int16", 2),
        ("EA::Reflection::UInt32", 4),
        ("EA::Reflection::UInt8", 1),
        ("EA::Reflection::Int8", 1),
        ("EA::Reflection::Int64", 8),
        ("Attrib::Key", 4),
        ("Attrib::StringKey", 16),
        ("Attrib::Blob", 8),
        ("Attrib::RefSpec", 12),
        ("Attrib::Types::Symbol", 4),
        ("Attrib::Types::Colour", 4),
        ("Attrib::Types::FloatColour", 16),
        ("Attrib::Types::Matrix", 64),
        ("Attrib::Types::Quaternion", 16),
        ("Attrib::Types::Vector4", 16),
        ("Attrib::Types::Vector3", 12),
        ("Attrib::Types::Vector2", 8),
        ("AxlePair", 8),
        ("CarBodyMotion", 12),
        ("TrafficPatternRecord", 24),
        ("CopCountRecord", 24),
        ("CopFormationRecord", 12),
        ("AirSupport", 12),
        ("HeavySupport", 16),
        ("LeaderSupport", 20),
        ("DamageScaleRecord", 8),
        ("RoadNoiseRecord", 16),
        ("JunkmanMod", 12),
        ("FECarPartInfo", 12),
        ("EffectLinkageRecord", 32),
        ("TireEffectRecord", 20),
        ("CollisionReactionRecord", 24),
        ("AICollisionReactionRecord", 16),
    ]
}


# ---------------------------------------------------------------------------
# Data Model
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class VltFieldDef:
    key: int
    name: str
    type_hash: int
    type_name: str
    offset: int
    size: int
    max_count: int
    flags: int
    alignment: int

    @property
    def is_array(self) -> bool:
        return bool(self.flags & DEF_FLAG_ARRAY)

    @property
    def in_layout(self) -> bool:
        return bool(self.flags & DEF_FLAG_IN_LAYOUT)

    @property
    def is_bound(self) -> bool:
        return bool(self.flags & DEF_FLAG_IS_BOUND)

    @property
    def is_static(self) -> bool:
        return bool(self.flags & DEF_FLAG_STATIC)


@dataclasses.dataclass
class VltClassSchema:
    class_hash: int
    name: str
    collection_reserve: int
    layout_size: int
    layout_key_shift: int
    layout_count: int
    definitions: List[VltFieldDef]
    vlt_export_offset: int
    defs_file_offset: int

    def __post_init__(self) -> None:
        self.by_key: Dict[int, VltFieldDef] = {d.key: d for d in self.definitions}
        self.by_name: Dict[str, VltFieldDef] = {d.name: d for d in self.definitions}

    def find_field(self, field_ref: str) -> Optional[VltFieldDef]:
        if field_ref in self.by_name:
            return self.by_name[field_ref]
        low = field_ref.lower()
        for d in self.definitions:
            if d.name.lower() == low:
                return d
        h = _parse_hash_or_name(field_ref)
        return self.by_key.get(h)


@dataclasses.dataclass
class VltFieldValue:
    key: int
    name: str
    type_hash: int
    type_name: str
    storage: str
    file_offset: int
    size: int
    is_array: bool
    value: Any
    array_alloc: Optional[int] = None
    array_count: Optional[int] = None
    array_pad: int = 0
    element_offsets: Optional[List[int]] = None
    inherited_from: Optional[str] = None

    def to_dict(self) -> dict:
        d: Dict[str, Any] = {
            "name": self.name,
            "key": f"0x{self.key:08x}",
            "type": self.type_name,
            "storage": self.storage,
            "file_offset": f"0x{self.file_offset:08x}",
            "size": self.size,
            "is_array": self.is_array,
            "value": self.value,
        }
        if self.is_array:
            d["array_alloc"] = self.array_alloc
            d["array_count"] = self.array_count
            if self.array_pad:
                d["array_pad"] = self.array_pad
            if self.element_offsets is not None:
                d["element_offsets"] = [f"0x{o:08x}" for o in self.element_offsets]
        if self.inherited_from is not None:
            d["inherited_from"] = self.inherited_from
        return d


@dataclasses.dataclass
class VltCollection:
    vault_name: str
    key: int
    name: str
    class_hash: int
    class_name: str
    parent_key: int
    parent_name: Optional[str]
    vlt_export_offset: int
    layout_file_offset: Optional[int]
    fields: Dict[str, VltFieldValue] = dataclasses.field(default_factory=dict)
    fields_by_key: Dict[int, VltFieldValue] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class VltVault:
    index: int
    name: str
    bin_offset: int
    bin_size: int
    vlt_offset: int
    vlt_size: int
    chunks: List[dict]
    dependencies: List[str]
    vlt_ptrs: Dict[int, int]
    bin_ptrs: Dict[int, int]
    types_by_hash: Dict[int, Tuple[str, int]]
    classes: Dict[int, VltClassSchema]
    collections: List[VltCollection]
    collections_by_class: Dict[int, List[VltCollection]]
    str_table_offset: Optional[int]
    str_table_size: int


def _parse_hash_or_name(ref: str) -> int:
    s = ref.strip()
    if s.lower().startswith("0x"):
        return int(s, 16) & 0xFFFFFFFF
    return vlt32_hash(s)


def _read_cstr(buf: bytes, offset: int, limit: Optional[int] = None) -> str:
    if offset < 0 or offset >= len(buf):
        return ""
    end = len(buf) if limit is None else min(len(buf), limit)
    nul = buf.find(b"\x00", offset, end)
    if nul == -1:
        nul = end
    return buf[offset:nul].decode("ascii", errors="replace")


def _parse_ptr_chunk(vlt: bytes, chunk_rel_off: int, chunk_size: int) -> Tuple[Dict[int, int], Dict[int, int]]:
    """Parse a PtrN chunk into (vlt_ptrs, bin_ptrs) mapping stream_offset -> bin_offset."""
    vlt_ptrs: Dict[int, int] = {}
    bin_ptrs: Dict[int, int] = {}
    pos = chunk_rel_off + 8
    end = chunk_rel_off + chunk_size
    is_vlt = False
    while pos + 12 <= end:
        fixup_off, ptype, idx, dst = struct.unpack_from("<IHHI", vlt, pos)
        pos += 12
        if ptype == PTR_END:
            break
        if ptype == PTR_SET_FIXUP_TARGET:
            is_vlt = idx == 0
        elif ptype in (PTR_NULL, PTR_DEP_RELATIVE):
            val = 0 if ptype == PTR_NULL else dst
            if is_vlt:
                vlt_ptrs[fixup_off] = val
            else:
                bin_ptrs[fixup_off] = val
    return vlt_ptrs, bin_ptrs


def _parse_dep_chunk(vlt: bytes, chunk_rel_off: int, chunk_size: int) -> List[str]:
    """Parse a DepN chunk to extract dependency names (e.g. ['db.vlt', 'db.bin'])."""
    if chunk_size < 12:
        return []
    dep_count = struct.unpack_from("<I", vlt, chunk_rel_off + 8)[0]
    header_end = chunk_rel_off + 12 + dep_count * 8  # dep_count u32 hashes + dep_count u32 name offsets
    if header_end > chunk_rel_off + chunk_size:
        return []
    name_offs = struct.unpack_from(f"<{dep_count}I", vlt, chunk_rel_off + 12 + dep_count * 4)
    names: List[str] = []
    chunk_end = chunk_rel_off + chunk_size
    for noff in name_offs:
        names.append(_read_cstr(vlt, header_end + noff, chunk_end))
    return names


# ---------------------------------------------------------------------------
# Full VPAK / VLT Parser
# ---------------------------------------------------------------------------


class VpakDatabase:
    """Complete read-only model of a PS2 NFSMW VPAK archive with exact file byte offsets."""

    def __init__(
        self,
        data: bytes,
        schema_source: Optional["VpakDatabase"] = None,
        extra_names: Optional[Sequence[str]] = None,
    ) -> None:
        if len(data) < 16 or data[:4] != b"VPAK":
            raise ValueError(f"Not a VPAK container (magic={data[:4]!r})")
        self.data = data
        self.name_map: Dict[int, str] = build_default_name_map(extra_names)
        if schema_source is not None:
            self.name_map.update(schema_source.name_map)

        self.types_by_hash: Dict[int, Tuple[str, int]] = dict(BUILTIN_TYPES)
        self.classes_by_hash: Dict[int, VltClassSchema] = {}
        if schema_source is not None:
            self.types_by_hash.update(schema_source.types_by_hash)
            self.classes_by_hash.update(schema_source.classes_by_hash)

        self.vaults: List[VltVault] = []
        self._parse_all()

    def resolve_hash(self, h: int) -> str:
        return self.name_map.get(h, f"0x{h:08x}")

    def _parse_all(self) -> None:
        data = self.data
        _, num_vaults, str_off, str_size = struct.unpack_from("<4sIII", data, 0)

        # First pass: read vault headers, chunks, StrE strings, pointers, DatabaseLoadData & ClassLoadData
        raw_vault_metas = []
        for i in range(num_vaults):
            name_off, bin_size, vlt_size, bin_off, vlt_off = struct.unpack_from(
                "<5I", data, 16 + i * 20
            )
            vname = _read_cstr(data, str_off + name_off, str_off + str_size) or f"vault_{i}"
            vlt = data[vlt_off : vlt_off + vlt_size]
            bin_data = data[bin_off : bin_off + bin_size]

            # Harvest StrE strings into name_map
            str_table_off: Optional[int] = None
            str_table_sz = 0
            if len(bin_data) >= 8 and bin_data[:4] == b"ErtS":
                str_table_off = bin_off
                str_table_sz = struct.unpack_from("<I", bin_data, 4)[0]
                if 8 <= str_table_sz <= len(bin_data):
                    for raw_s in bin_data[8:str_table_sz].split(b"\x00"):
                        if raw_s:
                            s = raw_s.decode("ascii", errors="ignore")
                            if s:
                                self.name_map.setdefault(vlt32_hash(s), s)

            # Walk VLT chunks
            chunks: List[dict] = []
            pos = 0
            while pos + 8 <= len(vlt):
                tag = vlt[pos : pos + 4][::-1].decode("ascii", errors="replace")
                sz = struct.unpack_from("<I", vlt, pos + 4)[0]
                if sz < 8 or pos + sz > len(vlt):
                    break
                chunks.append(
                    {
                        "tag": tag,
                        "offset": vlt_off + pos,
                        "rel_offset": pos,
                        "size": sz,
                    }
                )
                pos += sz

            dep_chunk = next((c for c in chunks if c["tag"] == "DepN"), None)
            deps = _parse_dep_chunk(vlt, dep_chunk["rel_offset"], dep_chunk["size"]) if dep_chunk else []

            ptr_chunk = next((c for c in chunks if c["tag"] == "PtrN"), None)
            vlt_ptrs, bin_ptrs = (
                _parse_ptr_chunk(vlt, ptr_chunk["rel_offset"], ptr_chunk["size"])
                if ptr_chunk
                else ({}, {})
            )

            # Parse ExpN entries
            exp_chunk = next((c for c in chunks if c["tag"] == "ExpN"), None)
            exports: List[Tuple[int, int, int, int, int]] = []
            if exp_chunk and exp_chunk["size"] >= 12:
                epos = exp_chunk["rel_offset"] + 8
                exp_count = struct.unpack_from("<I", vlt, epos)[0]
                exp_chunk["export_count"] = exp_count
                cursor = epos + 4
                chunk_end = exp_chunk["rel_offset"] + exp_chunk["size"]
                for _ in range(exp_count):
                    if cursor + 20 > chunk_end:
                        break
                    exports.append(struct.unpack_from("<5I", vlt, cursor))
                    cursor += 20

            # Parse DatabaseLoadData (types)
            vault_types: Dict[int, Tuple[str, int]] = {}
            for eid, etype, _zero, esz, eoff in exports:
                if etype == EXPORT_TYPE_DATABASE and eoff + 16 <= len(vlt):
                    _n_cls, _max_tsz, n_types, _ = struct.unpack_from("<4I", vlt, eoff)
                    type_names_bin_off = vlt_ptrs.get(eoff + 12, 0)
                    if type_names_bin_off and eoff + 16 + n_types * 4 <= len(vlt):
                        type_sizes = struct.unpack_from(f"<{n_types}I", vlt, eoff + 16)
                        tpos = type_names_bin_off
                        for ti in range(n_types):
                            nul = bin_data.find(b"\x00", tpos)
                            if nul == -1:
                                break
                            tname = bin_data[tpos:nul].decode("ascii", errors="replace")
                            tpos = nul + 1
                            thash = vlt32_hash(tname)
                            vault_types[thash] = (tname, type_sizes[ti])
                            self.types_by_hash[thash] = (tname, type_sizes[ti])

            # Parse ClassLoadData
            vault_classes: Dict[int, VltClassSchema] = {}
            for eid, etype, _zero, esz, eoff in exports:
                if etype == EXPORT_TYPE_CLASS and eoff + 28 <= len(vlt):
                    (
                        m_class,
                        m_col_res,
                        m_num_defs,
                        _raw_defs_ptr,
                        m_layout_sz,
                        m_layout_shift,
                        m_layout_cnt,
                    ) = struct.unpack_from("<7I", vlt, eoff)
                    defs_bin_off = vlt_ptrs.get(eoff + 12, 0)
                    defs: List[VltFieldDef] = []
                    if defs_bin_off:
                        for d_idx in range(m_num_defs):
                            doff_in_bin = defs_bin_off + d_idx * 16
                            if doff_in_bin + 16 > len(bin_data):
                                break
                            dk, dt, foff, fsz, fmax, fflags, falign_log2 = struct.unpack_from(
                                "<IIHHHBB", bin_data, doff_in_bin
                            )
                            tname, _tsz = self.types_by_hash.get(dt, (f"0x{dt:08x}", fsz))
                            defs.append(
                                VltFieldDef(
                                    key=dk,
                                    name=self.resolve_hash(dk),
                                    type_hash=dt,
                                    type_name=tname,
                                    offset=foff,
                                    size=fsz,
                                    max_count=fmax,
                                    flags=fflags,
                                    alignment=1 << falign_log2,
                                )
                            )
                    schema = VltClassSchema(
                        class_hash=m_class,
                        name=self.resolve_hash(m_class),
                        collection_reserve=m_col_res,
                        layout_size=m_layout_sz,
                        layout_key_shift=m_layout_shift,
                        layout_count=m_layout_cnt,
                        definitions=defs,
                        vlt_export_offset=vlt_off + eoff,
                        defs_file_offset=bin_off + defs_bin_off if defs_bin_off else 0,
                    )
                    vault_classes[m_class] = schema
                    self.classes_by_hash[m_class] = schema

            raw_vault_metas.append(
                (
                    i,
                    vname,
                    bin_off,
                    bin_size,
                    vlt_off,
                    vlt_size,
                    chunks,
                    deps,
                    vlt_ptrs,
                    bin_ptrs,
                    vault_types,
                    vault_classes,
                    exports,
                    str_table_off,
                    str_table_sz,
                )
            )

        # Second pass: decode all CollectionLoadData nodes now that all classes/types are loaded
        col_name_key = vlt32_hash("CollectionName")
        for meta in raw_vault_metas:
            (
                i,
                vname,
                bin_off,
                bin_size,
                vlt_off,
                vlt_size,
                chunks,
                deps,
                vlt_ptrs,
                bin_ptrs,
                vault_types,
                vault_classes,
                exports,
                str_table_off,
                str_table_sz,
            ) = meta
            vlt = data[vlt_off : vlt_off + vlt_size]
            bin_data = data[bin_off : bin_off + bin_size]

            collections: List[VltCollection] = []
            cols_by_class: Dict[int, List[VltCollection]] = {}

            for eid, etype, _zero, esz, eoff in exports:
                if etype != EXPORT_TYPE_COLLECTION or eoff + 32 > len(vlt):
                    continue
                (
                    m_key,
                    m_class,
                    m_parent,
                    _m_reserve,
                    _m_shift,
                    m_num_entries,
                    m_num_types,
                    _raw_layout_ptr,
                ) = struct.unpack_from("<8I", vlt, eoff)

                layout_bin_off = vlt_ptrs.get(eoff + 28, 0)
                layout_file_off = (bin_off + layout_bin_off) if layout_bin_off else None
                cls_schema = self.classes_by_hash.get(m_class)

                col = VltCollection(
                    vault_name=vname,
                    key=m_key,
                    name=self.resolve_hash(m_key),
                    class_hash=m_class,
                    class_name=cls_schema.name if cls_schema else self.resolve_hash(m_class),
                    parent_key=m_parent,
                    parent_name=self.resolve_hash(m_parent) if m_parent else None,
                    vlt_export_offset=vlt_off + eoff,
                    layout_file_offset=layout_file_off,
                )

                # Decode required (InLayout) fields from .bin layout block
                if layout_bin_off and cls_schema is not None:
                    for fdef in cls_schema.definitions:
                        if not fdef.in_layout:
                            continue
                        field_bin_off = layout_bin_off + fdef.offset
                        if fdef.is_array:
                            fv = self._decode_array_in_bin(
                                fdef.key,
                                fdef.name,
                                fdef.type_hash,
                                fdef.type_name,
                                fdef.size,
                                bin_off,
                                bin_data,
                                field_bin_off,
                                bin_ptrs,
                                storage="layout_array",
                            )
                        else:
                            val = self._decode_scalar(
                                fdef.type_name,
                                fdef.size,
                                bin_data,
                                field_bin_off,
                                bin_ptrs,
                                bin_data,
                            )
                            fv = VltFieldValue(
                                key=fdef.key,
                                name=fdef.name,
                                type_hash=fdef.type_hash,
                                type_name=fdef.type_name,
                                storage="layout",
                                file_offset=bin_off + field_bin_off,
                                size=fdef.size,
                                is_array=False,
                                value=val,
                            )
                        col.fields[fv.name] = fv
                        col.fields_by_key[fv.key] = fv

                # Decode optional AttribEntry fields
                types_pos = eoff + 32
                tlist = (
                    struct.unpack_from(f"<{m_num_types}I", vlt, types_pos)
                    if m_num_types and types_pos + 4 * m_num_types <= len(vlt)
                    else ()
                )
                ent_pos = types_pos + 4 * m_num_types
                for j in range(m_num_entries):
                    cur = ent_pos + j * 12
                    if cur + 12 > len(vlt):
                        break
                    ek, _raw_edata, etidx, nflags, _eflags = struct.unpack_from("<IIHBB", vlt, cur)
                    fdef = cls_schema.by_key.get(ek) if cls_schema else None
                    thash = tlist[etidx] if etidx < len(tlist) else (fdef.type_hash if fdef else 0)
                    tname, default_tsz = self.types_by_hash.get(
                        thash, (fdef.type_name if fdef else f"0x{thash:08x}", fdef.size if fdef else 4)
                    )
                    fsz = fdef.size if fdef else default_tsz
                    fname = fdef.name if fdef else self.resolve_hash(ek)
                    data_vlt_off = cur + 4
                    has_vlt_ptr = data_vlt_off in vlt_ptrs

                    if nflags & NODE_FLAG_IS_BY_VALUE:
                        if has_vlt_ptr:
                            # Inline pointer (e.g. EA::Reflection::Text pointing into StrE in .bin)
                            target_bin_off = vlt_ptrs[data_vlt_off]
                            val = _read_cstr(bin_data, target_bin_off) if target_bin_off else ""
                            fv = VltFieldValue(
                                key=ek,
                                name=fname,
                                type_hash=thash,
                                type_name=tname,
                                storage="inline_ptr",
                                file_offset=bin_off + target_bin_off if target_bin_off else vlt_off + data_vlt_off,
                                size=fsz,
                                is_array=False,
                                value=val,
                            )
                        else:
                            val = self._decode_scalar(
                                tname,
                                fsz,
                                vlt,
                                data_vlt_off,
                                vlt_ptrs,
                                bin_data,
                            )
                            fv = VltFieldValue(
                                key=ek,
                                name=fname,
                                type_hash=thash,
                                type_name=tname,
                                storage="inline",
                                file_offset=vlt_off + data_vlt_off,
                                size=fsz,
                                is_array=False,
                                value=val,
                            )
                    elif nflags & NODE_FLAG_IS_ARRAY:
                        arr_bin_off = vlt_ptrs.get(data_vlt_off, 0)
                        fv = self._decode_array_in_bin(
                            ek,
                            fname,
                            thash,
                            tname,
                            fsz,
                            bin_off,
                            bin_data,
                            arr_bin_off,
                            bin_ptrs,
                            storage="bin_array",
                        )
                    else:
                        target_bin_off = vlt_ptrs.get(data_vlt_off, 0)
                        val = (
                            self._decode_scalar(
                                tname,
                                fsz,
                                bin_data,
                                target_bin_off,
                                bin_ptrs,
                                bin_data,
                            )
                            if target_bin_off
                            else None
                        )
                        fv = VltFieldValue(
                            key=ek,
                            name=fname,
                            type_hash=thash,
                            type_name=tname,
                            storage="bin",
                            file_offset=bin_off + target_bin_off if target_bin_off else vlt_off + data_vlt_off,
                            size=fsz,
                            is_array=False,
                            value=val,
                        )

                    col.fields[fv.name] = fv
                    col.fields_by_key[fv.key] = fv

                # Auto-discover collection name from CollectionName field when hash wasn't known
                cn_field = col.fields_by_key.get(col_name_key)
                if cn_field and isinstance(cn_field.value, str) and cn_field.value:
                    if col.name.startswith("0x"):
                        col.name = cn_field.value
                    self.name_map.setdefault(col.key, cn_field.value)

                collections.append(col)
                cols_by_class.setdefault(m_class, []).append(col)

            # Second quick pass inside vault to update any parent_name discovered via CollectionName
            for col in collections:
                if col.parent_key and (not col.parent_name or col.parent_name.startswith("0x")):
                    col.parent_name = self.resolve_hash(col.parent_key)

            self.vaults.append(
                VltVault(
                    index=i,
                    name=vname,
                    bin_offset=bin_off,
                    bin_size=bin_size,
                    vlt_offset=vlt_off,
                    vlt_size=vlt_size,
                    chunks=chunks,
                    dependencies=deps,
                    vlt_ptrs=vlt_ptrs,
                    bin_ptrs=bin_ptrs,
                    types_by_hash=vault_types,
                    classes=vault_classes,
                    collections=collections,
                    collections_by_class=cols_by_class,
                    str_table_offset=str_table_off,
                    str_table_size=str_table_sz,
                )
            )

    def _decode_array_in_bin(
        self,
        key: int,
        name: str,
        type_hash: int,
        type_name: str,
        def_size: int,
        bin_off: int,
        bin_data: bytes,
        arr_bin_off: int,
        bin_ptrs: Dict[int, int],
        storage: str,
    ) -> VltFieldValue:
        if arr_bin_off == 0 or arr_bin_off + 8 > len(bin_data):
            return VltFieldValue(
                key=key,
                name=name,
                type_hash=type_hash,
                type_name=type_name,
                storage=storage,
                file_offset=bin_off + arr_bin_off,
                size=def_size,
                is_array=True,
                value=[],
                array_alloc=0,
                array_count=0,
                array_pad=0,
                element_offsets=[],
            )
        m_alloc, m_count, m_size, m_encoded = struct.unpack_from("<HHHH", bin_data, arr_bin_off)
        elem_size = m_size if m_size > 0 else def_size
        pad = 8 if (m_encoded & ARRAY_FLAG_ALIGNED_16) else 0
        elem_base = arr_bin_off + 8 + pad

        items: List[Any] = []
        elem_file_offs: List[int] = []
        for idx in range(m_count):
            eoff = elem_base + idx * elem_size
            if eoff + elem_size > len(bin_data):
                break
            elem_file_offs.append(bin_off + eoff)
            items.append(self._decode_scalar(type_name, elem_size, bin_data, eoff, bin_ptrs, bin_data))

        return VltFieldValue(
            key=key,
            name=name,
            type_hash=type_hash,
            type_name=type_name,
            storage=storage,
            file_offset=bin_off + arr_bin_off,
            size=elem_size,
            is_array=True,
            value=items,
            array_alloc=m_alloc,
            array_count=m_count,
            array_pad=pad,
            element_offsets=elem_file_offs,
        )

    def _decode_scalar(
        self,
        type_name: str,
        size: int,
        buf: bytes,
        offset: int,
        ptr_map: Dict[int, int],
        bin_data: bytes,
    ) -> Any:
        if offset < 0 or offset + size > len(buf):
            return None

        # Standard numeric primitives
        if type_name in PRIMITIVE_STRUCT_FMTS:
            fmt, expected_sz = PRIMITIVE_STRUCT_FMTS[type_name]
            if size >= expected_sz:
                val = struct.unpack_from(fmt, buf, offset)[0]
                return round(val, 6) if isinstance(val, float) and math.isfinite(val) else val

        if type_name == "EA::Reflection::Bool":
            if size == 4:
                return bool(struct.unpack_from("<I", buf, offset)[0])
            if size == 1:
                return bool(struct.unpack_from("<B", buf, offset)[0])

        if type_name == "EA::Reflection::Text":
            str_bin_off = ptr_map.get(offset, 0)
            return _read_cstr(bin_data, str_bin_off) if str_bin_off else ""

        if type_name == "Attrib::StringKey" and size >= 16:
            h64, h32, _raw_ptr = struct.unpack_from("<QII", buf, offset)
            str_bin_off = ptr_map.get(offset + 12, 0)
            text = _read_cstr(bin_data, str_bin_off) if str_bin_off else ""
            return {
                "text": text,
                "hash32": f"0x{h32:08x}",
                "hash64": f"0x{h64:016x}",
            }

        if type_name == "Attrib::RefSpec" and size >= 12:
            cls_h, col_h, _pad = struct.unpack_from("<III", buf, offset)
            return {
                "class": self.resolve_hash(cls_h) if cls_h else "",
                "class_hash": f"0x{cls_h:08x}",
                "collection": self.resolve_hash(col_h) if col_h else "",
                "collection_hash": f"0x{col_h:08x}",
            }

        if type_name in VECTOR_TYPES:
            n = VECTOR_TYPES[type_name]
            if size >= n * 4:
                vals = struct.unpack_from(f"<{n}f", buf, offset)
                return [round(v, 6) if math.isfinite(v) else v for v in vals]

        if type_name == "Attrib::Types::Matrix" and size >= 64:
            vals = struct.unpack_from("<16f", buf, offset)
            rounded = [round(v, 6) if math.isfinite(v) else v for v in vals]
            return [rounded[r * 4 : (r + 1) * 4] for r in range(4)]

        if type_name == "Attrib::Types::Colour" and size >= 4:
            return list(struct.unpack_from("<4B", buf, offset))

        if type_name in STRUCT_SCHEMAS:
            out: Dict[str, Any] = {}
            for mname, mtype, moff in STRUCT_SCHEMAS[type_name]:
                msz = _sizeof_type(mtype)
                out[mname] = self._decode_scalar(mtype, msz, buf, offset + moff, ptr_map, bin_data)
            return out

        # Hash / Key / Enum types (4-byte uint32)
        if size == 4 and (
            type_name in ("Attrib::Key", "Attrib::Types::Symbol", "GCollectionKey", "type_bStringHash")
            or "::" in type_name
            or type_name.startswith(("e", "Formation", "Surface", "PathEvent", "Input", "EffectParticle", "Air", "Heavy", "Leader", "STICH_", "WORLDOBJECT_", "FXROADNOISE_", "SPCH"))
        ):
            raw_u32 = struct.unpack_from("<I", buf, offset)[0]
            if type_name in ("Attrib::Key", "Attrib::Types::Symbol", "GCollectionKey") and raw_u32 in self.name_map:
                return {"value": raw_u32, "hex": f"0x{raw_u32:08x}", "resolved": self.name_map[raw_u32]}
            return raw_u32

        # Fallback: raw hex bytes
        return {"raw_hex": buf[offset : offset + size].hex()}

    # -----------------------------------------------------------------------
    # Query Helpers
    # -----------------------------------------------------------------------

    def list_classes(self, include_external_empty: bool = False) -> List[dict]:
        """List all classes & collection counts across all vaults."""
        counts: Dict[int, int] = {}
        local_classes: set[int] = set()
        for v in self.vaults:
            local_classes.update(v.classes.keys())
            for chash, cols in v.collections_by_class.items():
                counts[chash] = counts.get(chash, 0) + len(cols)
        if include_external_empty:
            all_hashes = set(self.classes_by_hash.keys()) | set(counts.keys())
        else:
            all_hashes = local_classes | set(counts.keys())
        rows: List[dict] = []
        for chash in all_hashes:
            schema = self.classes_by_hash.get(chash)
            name = schema.name if schema else self.resolve_hash(chash)
            rows.append(
                {
                    "class": name,
                    "hash": f"0x{chash:08x}",
                    "definitions": len(schema.definitions) if schema else 0,
                    "layout_size": schema.layout_size if schema else 0,
                    "layout_fields": schema.layout_count if schema else 0,
                    "collections": counts.get(chash, 0),
                }
            )
        rows.sort(key=lambda r: r["class"])
        return rows

    def list_nodes(self, class_ref: str, vault_name: Optional[str] = None) -> List[VltCollection]:
        chash = _parse_hash_or_name(class_ref)
        out: List[VltCollection] = []
        for v in self.vaults:
            if vault_name and v.name != vault_name:
                continue
            out.extend(v.collections_by_class.get(chash, []))
        return out

    def find_collection(
        self,
        class_ref: str,
        node_ref: str,
        vault_name: Optional[str] = None,
    ) -> VltCollection:
        cols = self.list_nodes(class_ref, vault_name=vault_name)
        if not cols:
            raise KeyError(f"No collections found for class {class_ref!r}" + (f" in vault {vault_name!r}" if vault_name else ""))
        target_hash = _parse_hash_or_name(node_ref)
        low = node_ref.strip().lower()
        # 1. Exact key hash match
        for c in cols:
            if c.key == target_hash:
                return c
        # 2. Exact name / CollectionName match (case-insensitive)
        for c in cols:
            if c.name.lower() == low:
                return c
        raise KeyError(
            f"Collection node {node_ref!r} (hash 0x{target_hash:08x}) not found in class {class_ref!r}"
            + (f" (vault {vault_name!r})" if vault_name else "")
        )

    def get_effective_fields(
        self, col: VltCollection, include_inherited: bool = False
    ) -> Dict[str, VltFieldValue]:
        """Return fields of a collection, optionally merging inherited parent fields."""
        if not include_inherited:
            return dict(col.fields)

        # Build parent chain from root -> col
        chain: List[VltCollection] = [col]
        seen_keys = {col.key}
        cur = col
        while cur.parent_key and cur.parent_key not in seen_keys:
            try:
                parent = self.find_collection(
                    f"0x{cur.class_hash:08x}", f"0x{cur.parent_key:08x}", vault_name=cur.vault_name
                )
            except KeyError:
                break
            chain.append(parent)
            seen_keys.add(parent.key)
            cur = parent

        merged: Dict[str, VltFieldValue] = {}
        for ancestor in reversed(chain):
            for fname, fv in ancestor.fields.items():
                if ancestor is col:
                    merged[fname] = fv
                else:
                    merged[fname] = dataclasses.replace(fv, inherited_from=ancestor.name)
        return merged


def _sizeof_type(type_name: str) -> int:
    if type_name in PRIMITIVE_STRUCT_FMTS:
        return PRIMITIVE_STRUCT_FMTS[type_name][1]
    if type_name == "EA::Reflection::Bool":
        return 4
    if type_name == "EA::Reflection::Text":
        return 4
    if type_name == "Attrib::RefSpec":
        return 12
    if type_name == "Attrib::StringKey":
        return 16
    if type_name in VECTOR_TYPES:
        return VECTOR_TYPES[type_name] * 4
    if type_name == "Attrib::Types::Matrix":
        return 64
    if type_name == "Attrib::Types::Colour":
        return 4
    h = vlt32_hash(type_name)
    if h in BUILTIN_TYPES:
        return BUILTIN_TYPES[h][1]
    return 4


# ---------------------------------------------------------------------------
# Backwards-Compatible Summary Parser
# ---------------------------------------------------------------------------


def parse_vpak(data: bytes) -> dict:
    """Parse VPAK container header, chunks, exports & known class summary (backwards-compatible)."""
    if len(data) < 16 or data[:4] != b"VPAK":
        raise ValueError(f"Not a VPAK container (magic={data[:4]!r})")
    _, num_vaults, str_off, str_size = struct.unpack_from("<4sIII", data, 0)
    vaults: List[dict] = []

    for i in range(num_vaults):
        name_off, bin_size, vlt_size, bin_off, vlt_off = struct.unpack_from(
            "<5I", data, 16 + i * 20
        )
        name_pos = str_off + name_off
        nul = data.find(b"\x00", name_pos)
        vname = (
            data[name_pos:nul].decode("ascii", errors="replace")
            if nul != -1
            else f"vault_{i}"
        )

        chunks = []
        pos = vlt_off
        end = vlt_off + vlt_size
        resolved_classes: List[dict] = []

        while pos + 8 <= end:
            raw_tag = data[pos : pos + 4]
            tag = raw_tag[::-1].decode("ascii", errors="replace")
            sz = struct.unpack_from("<I", data, pos + 4)[0]
            if sz < 8 or pos + sz > end:
                break
            cinfo: Dict[str, Any] = {"tag": tag, "offset": pos, "size": sz}

            if tag == "ExpN":
                exp_count = struct.unpack_from("<I", data, pos + 8)[0]
                cinfo["export_count"] = exp_count
                epos = pos + 12
                for _ in range(exp_count):
                    if epos + 20 > pos + sz:
                        break
                    # 32-bit VLT ExpN entry: (id:u32, type:u32, zero:u32, size:u32, vlt_offset:u32)
                    eid, etype, _zero, esize, eoff = struct.unpack_from("<5I", data, epos)
                    if etype == EXPORT_TYPE_CLASS and eid in VLT_CLASS_HASHES:
                        resolved_classes.append(
                            {
                                "class": VLT_CLASS_HASHES[eid],
                                "hash": f"0x{eid:08x}",
                                "export_type": f"0x{etype:08x}",
                                "size": esize,
                                "vlt_offset": eoff,
                            }
                        )
                    epos += 20
            elif tag == "DepN":
                dep_count = struct.unpack_from("<I", data, pos + 8)[0]
                cinfo["dependency_count"] = dep_count

            chunks.append(cinfo)
            pos += sz

        dat_chunk = next((c for c in chunks if c["tag"] == "DatN"), None)
        found_in_dat = set()
        if dat_chunk:
            dstart = dat_chunk["offset"] + 8
            dend = dat_chunk["offset"] + dat_chunk["size"]
            for off in range(dstart, dend - 3, 4):
                w = struct.unpack_from("<I", data, off)[0]
                if w in VLT_CLASS_HASHES:
                    found_in_dat.add(VLT_CLASS_HASHES[w])

        vaults.append(
            {
                "index": i,
                "name": vname,
                "bin_offset": bin_off,
                "bin_size": bin_size,
                "vlt_offset": vlt_off,
                "vlt_size": vlt_size,
                "chunks": chunks,
                "resolved_exports": resolved_classes,
                "known_classes_detected": sorted(found_in_dat),
            }
        )

    return {
        "magic": "VPAK",
        "vault_count": num_vaults,
        "total_size": len(data),
        "vaults": vaults,
    }


# ---------------------------------------------------------------------------
# Verified In-Place JSON Recipe Patcher
# ---------------------------------------------------------------------------


def _values_match(type_name: str, actual: Any, expected: Any, float_tol: float = 1e-4) -> bool:
    if type_name in ("EA::Reflection::Float", "EA::Reflection::Double"):
        if not isinstance(actual, (int, float)) or not isinstance(expected, (int, float)):
            return False
        return math.isclose(float(actual), float(expected), rel_tol=float_tol, abs_tol=float_tol)

    if type_name == "EA::Reflection::Bool":
        return bool(actual) == bool(expected)

    if type_name in VECTOR_TYPES:
        if not isinstance(actual, list) or not isinstance(expected, list) or len(actual) != len(expected):
            return False
        return all(
            math.isclose(float(a), float(e), rel_tol=float_tol, abs_tol=float_tol)
            for a, e in zip(actual, expected)
        )

    if type_name == "Attrib::Types::Matrix":
        if not isinstance(actual, list) or not isinstance(expected, list) or len(actual) != 4 or len(expected) != 4:
            return False
        return all(
            len(ar) == 4
            and len(er) == 4
            and all(math.isclose(float(a), float(e), rel_tol=float_tol, abs_tol=float_tol) for a, e in zip(ar, er))
            for ar, er in zip(actual, expected)
        )

    if type_name == "Attrib::RefSpec":
        if not isinstance(actual, dict) or not isinstance(expected, dict):
            return False
        exp_cls = _parse_hash_or_name(expected["class"]) if "class" in expected and expected["class"] else 0
        exp_col = _parse_hash_or_name(expected["collection"]) if "collection" in expected and expected["collection"] else 0
        act_cls = int(actual["class_hash"], 16)
        act_col = int(actual["collection_hash"], 16)
        return act_cls == exp_cls and act_col == exp_col

    if type_name == "Attrib::Types::Colour":
        return isinstance(actual, list) and isinstance(expected, list) and [int(x) for x in actual] == [int(x) for x in expected]

    if isinstance(actual, dict) and "value" in actual:
        exp_u32 = _parse_hash_or_name(expected) if isinstance(expected, str) else int(expected)
        return int(actual["value"]) == exp_u32

    if isinstance(expected, str) and isinstance(actual, int):
        return actual == _parse_hash_or_name(expected)

    return actual == expected


def _encode_scalar_bytes(type_name: str, size: int, value: Any) -> bytes:
    if type_name in PRIMITIVE_STRUCT_FMTS:
        fmt, expected_sz = PRIMITIVE_STRUCT_FMTS[type_name]
        if size < expected_sz:
            raise ValueError(f"Field size {size} too small for {type_name} ({expected_sz}B)")
        coerced = float(value) if fmt in ("<f", "<d") else int(value)
        return struct.pack(fmt, coerced)

    if type_name == "EA::Reflection::Bool":
        bval = 1 if bool(value) else 0
        if size == 4:
            return struct.pack("<I", bval)
        if size == 1:
            return struct.pack("<B", bval)
        raise ValueError(f"Unsupported Bool size {size}")

    if type_name in VECTOR_TYPES:
        n = VECTOR_TYPES[type_name]
        if not isinstance(value, (list, tuple)) or len(value) != n:
            raise ValueError(f"{type_name} requires a {n}-element list of floats")
        return struct.pack(f"<{n}f", *[float(v) for v in value])

    if type_name == "Attrib::Types::Matrix":
        if not isinstance(value, (list, tuple)) or len(value) != 4 or any(len(r) != 4 for r in value):
            raise ValueError("Attrib::Types::Matrix requires a 4x4 nested list of floats")
        flat = [float(v) for row in value for v in row]
        return struct.pack("<16f", *flat)

    if type_name == "Attrib::Types::Colour":
        if not isinstance(value, (list, tuple)) or len(value) != 4:
            raise ValueError("Attrib::Types::Colour requires [r, g, b, a]")
        return struct.pack("<4B", *[int(v) & 0xFF for v in value])

    if type_name == "Attrib::RefSpec":
        if not isinstance(value, dict):
            raise ValueError("Attrib::RefSpec requires {'class': ..., 'collection': ...}")
        cls_h = _parse_hash_or_name(value["class"]) if value.get("class") else 0
        col_h = _parse_hash_or_name(value["collection"]) if value.get("collection") else 0
        return struct.pack("<III", cls_h, col_h, 0)

    if size == 4:
        u32 = _parse_hash_or_name(value) if isinstance(value, str) else int(value)
        return struct.pack("<I", u32 & 0xFFFFFFFF)

    raise ValueError(f"In-place encoding not supported for type {type_name!r} (size={size})")


def _resolve_patch_target(
    db: VpakDatabase,
    patch: dict,
    default_vault: Optional[str],
) -> Tuple[int, str, int, Any, str]:
    """Resolve a single patch spec into (file_offset, leaf_type, leaf_size, actual_val, label)."""
    cls_ref = patch["class"]
    node_ref = patch["node"]
    field_ref = patch["field"]
    vault_name = patch.get("vault", default_vault)

    col = db.find_collection(cls_ref, node_ref, vault_name=vault_name)

    # Locate field strictly on the target collection (never silently mutate an inherited parent!)
    fv: Optional[VltFieldValue] = col.fields.get(field_ref)
    if fv is None:
        low = field_ref.lower()
        for k, v in col.fields.items():
            if k.lower() == low:
                fv = v
                break
    if fv is None:
        fh = _parse_hash_or_name(field_ref)
        fv = col.fields_by_key.get(fh)
    if fv is None:
        raise KeyError(
            f"Field {field_ref!r} is not locally stored on {col.class_name}/{col.name} "
            f"(vault={col.vault_name!r}). If inherited from parent {col.parent_name!r}, "
            f"patch the parent collection explicitly."
        )

    cur_off = fv.file_offset
    cur_type = fv.type_name
    cur_size = fv.size
    cur_val = fv.value
    label = f"{col.class_name}/{col.name}.{fv.name}"

    if fv.is_array:
        if "index" not in patch or patch["index"] is None:
            raise ValueError(f"Patch for array field {label} must specify 'index'")
        idx = int(patch["index"])
        if fv.element_offsets is None or idx < 0 or idx >= len(fv.element_offsets):
            raise IndexError(
                f"Array index {idx} out of range for {label} (count={fv.array_count})"
            )
        cur_off = fv.element_offsets[idx]
        cur_val = fv.value[idx]
        label += f"[{idx}]"
    elif patch.get("index") is not None and cur_type in VECTOR_TYPES:
        idx = int(patch["index"])
        dim = VECTOR_TYPES[cur_type]
        if idx < 0 or idx >= dim:
            raise IndexError(f"Vector index {idx} out of range for {label} ({cur_type})")
        cur_off = cur_off + idx * 4
        cur_type = "EA::Reflection::Float"
        cur_size = 4
        cur_val = cur_val[idx]
        label += f"[{idx}]"

    # Optional struct member or vector/matrix component selection
    member = patch.get("member")
    if member is not None:
        if cur_type in STRUCT_SCHEMAS:
            schema = STRUCT_SCHEMAS[cur_type]
            match = next((m for m in schema if m[0] == member or m[0].lower() == str(member).lower()), None)
            if match is None:
                valid = [m[0] for m in schema]
                raise KeyError(f"Unknown member {member!r} on {cur_type} ({label}); valid: {valid}")
            mname, mtype, moff = match
            cur_off += moff
            cur_type = mtype
            cur_size = _sizeof_type(mtype)
            cur_val = cur_val[mname]
            label += f".{mname}"
        elif cur_type in VECTOR_TYPES:
            comp_map = {"x": 0, "y": 1, "z": 2, "w": 3, "r": 0, "g": 1, "b": 2, "a": 3}
            idx = comp_map[member.lower()] if isinstance(member, str) and member.lower() in comp_map else int(member)
            dim = VECTOR_TYPES[cur_type]
            if idx < 0 or idx >= dim:
                raise IndexError(f"Component {member!r} out of range for {cur_type} ({label})")
            cur_off += idx * 4
            cur_type = "EA::Reflection::Float"
            cur_size = 4
            cur_val = cur_val[idx]
            label += f".{member}"
        elif cur_type == "Attrib::Types::Matrix":
            if not (isinstance(member, (list, tuple)) and len(member) == 2):
                raise ValueError(f"Matrix member must be [row, col] for {label}")
            r, c = int(member[0]), int(member[1])
            if not (0 <= r < 4 and 0 <= c < 4):
                raise IndexError(f"Matrix coordinate [{r}, {c}] out of range for {label}")
            cur_off += (r * 4 + c) * 4
            cur_type = "EA::Reflection::Float"
            cur_size = 4
            cur_val = cur_val[r][c]
            label += f"[{r}][{c}]"
        else:
            raise ValueError(f"Cannot address member {member!r} on non-composite type {cur_type} ({label})")

    return cur_off, cur_type, cur_size, cur_val, label


def apply_vlt_recipes(
    vpak_data: bytes,
    recipes: Sequence[dict],
    schema_source: Optional[VpakDatabase] = None,
) -> Tuple[bytes, List[dict]]:
    """
    Validate and apply one or more declarative JSON recipes to a VPAK binary buffer.

    SAFETY GUARANTEE:
    Every patch entry MUST specify `expected_orig`. All patches across all recipes are
    verified against the untouched input buffer BEFORE a single byte is modified.
    Raises ValueError / KeyError / IndexError and mutates nothing on any mismatch.
    """
    db = VpakDatabase(vpak_data, schema_source=schema_source)

    # Phase 1: Full pre-flight verification (strictly read-only!)
    planned_writes: List[Tuple[int, bytes, dict]] = []
    seen_ranges: List[Tuple[int, int, str]] = []

    for r_idx, recipe in enumerate(recipes):
        rid = recipe.get("id", f"recipe_{r_idx}")
        default_vault = recipe.get("target", {}).get("vault")
        patches = recipe.get("patches")
        if not isinstance(patches, list) or not patches:
            raise ValueError(f"Recipe {rid!r} contains no 'patches' list")

        for p_idx, patch in enumerate(patches):
            if "expected_orig" not in patch:
                raise ValueError(
                    f"Recipe {rid!r} patch #{p_idx} is missing mandatory 'expected_orig' field. "
                    f"Aborting without modifying any bytes."
                )
            if "new_value" not in patch:
                raise ValueError(
                    f"Recipe {rid!r} patch #{p_idx} is missing mandatory 'new_value' field."
                )

            file_off, leaf_type, leaf_size, actual_val, label = _resolve_patch_target(
                db, patch, default_vault
            )

            # Optional explicit byte-offset cross-check
            if "expected_offset" in patch and patch["expected_offset"] is not None:
                exp_off = (
                    int(patch["expected_offset"], 16)
                    if isinstance(patch["expected_offset"], str)
                    else int(patch["expected_offset"])
                )
                if file_off != exp_off:
                    raise ValueError(
                        f"Offset verification failed for {rid}:{label}: "
                        f"expected 0x{exp_off:08x}, resolved 0x{file_off:08x}. Aborting cleanly."
                    )

            expected_orig = patch["expected_orig"]
            if not _values_match(leaf_type, actual_val, expected_orig):
                orig_hex = vpak_data[file_off : file_off + leaf_size].hex()
                raise ValueError(
                    f"Safety verification failed for {rid}:{label} @ 0x{file_off:08x}: "
                    f"expected_orig={expected_orig!r}, actual={actual_val!r} (raw=0x{orig_hex}). "
                    f"Aborting cleanly without modifying a single byte."
                )

            encoded = _encode_scalar_bytes(leaf_type, leaf_size, patch["new_value"])
            if file_off < 0 or file_off + len(encoded) > len(vpak_data):
                raise ValueError(f"Write out of bounds for {rid}:{label} @ 0x{file_off:08x}")

            # Detect accidental overlapping writes across recipes
            w_end = file_off + len(encoded)
            for s_start, s_end, s_lbl in seen_ranges:
                if file_off < s_end and s_start < w_end:
                    raise ValueError(
                        f"Conflicting overlapping patch write at 0x{file_off:08x}: "
                        f"{rid}:{label} overlaps {s_lbl}"
                    )
            seen_ranges.append((file_off, w_end, f"{rid}:{label}"))

            orig_bytes = vpak_data[file_off : file_off + len(encoded)]
            planned_writes.append(
                (
                    file_off,
                    encoded,
                    {
                        "recipe_id": rid,
                        "target": label,
                        "type": leaf_type,
                        "file_offset": f"0x{file_off:08x}",
                        "size": len(encoded),
                        "orig_value": actual_val,
                        "new_value": patch["new_value"],
                        "orig_hex": orig_bytes.hex(),
                        "new_hex": encoded.hex(),
                    },
                )
            )

    # Phase 2: All checks passed -> apply atomically to a fresh bytearray
    out = bytearray(vpak_data)
    audit: List[dict] = []
    for file_off, encoded, log_entry in planned_writes:
        out[file_off : file_off + len(encoded)] = encoded
        audit.append(log_entry)

    return bytes(out), audit


def patch_memory_file_entry(
    memfile_data: bytes,
    virtual_path: str,
    new_payload: bytes,
) -> Tuple[bytes, dict]:
    """
    Update an embedded file inside an EA MemoryFile (e.g. GLOBAL\\GLOBALMEMORYFILE.BIN)
    in-place while preserving exact file size and layout.
    """
    if len(memfile_data) < 16:
        raise ValueError("MemoryFile buffer too short")
    _n_next, _n_prev, magic, count = struct.unpack_from("<4I", memfile_data, 0)
    if magic != MEMORY_FILE_MAGIC:
        raise ValueError(f"Not an EA MemoryFile (magic=0x{magic:08x}, expected 0x{MEMORY_FILE_MAGIC:08x})")

    norm = virtual_path.replace("/", "\\").upper()
    target_hash = bstringhash(norm)
    for i in range(count):
        h, off, fsz, msz, _ptr = struct.unpack_from("<5I", memfile_data, 16 + i * 20)
        if h == target_hash:
            if len(new_payload) != fsz or len(new_payload) != msz:
                raise ValueError(
                    f"In-place MemoryFile update requires exact size match for {norm}: "
                    f"entry has FileSize={fsz}, MemorySize={msz}, got {len(new_payload)}"
                )
            if off + fsz > len(memfile_data):
                raise ValueError(f"MemoryFile entry {norm} offset 0x{off:x} + {fsz} exceeds file length")
            out = bytearray(memfile_data)
            out[off : off + fsz] = new_payload
            return bytes(out), {
                "virtual_path": norm,
                "hash": f"0x{target_hash:08x}",
                "entry_index": i,
                "embedded_offset": f"0x{off:08x}",
                "size": fsz,
            }
    raise KeyError(f"Entry {norm!r} (0x{target_hash:08x}) not found in MemoryFile")


# ---------------------------------------------------------------------------
# Source Loading Helpers (Strictly Read-Only ISO Access)
# ---------------------------------------------------------------------------


def _load_vpak_source(
    source: Path,
    virtual_path: str = r"GLOBAL\ATTRIBUTES.BIN",
    schema_bin: Optional[Path] = None,
) -> Tuple[bytes, Optional[VpakDatabase]]:
    """Read raw VPAK bytes from an ISO (read-only 'rb') or extracted .bin file."""
    schema_db: Optional[VpakDatabase] = None
    if source.suffix.lower() == ".iso":
        arc = PS2NFSMWArchive(source)
        entry = arc.find(virtual_path)
        raw = arc.read_entry(entry)
        norm = virtual_path.replace("/", "\\").upper()
        if norm != r"GLOBAL\ATTRIBUTES.BIN" and schema_bin is None:
            # Automatically load class definitions from GLOBAL\ATTRIBUTES.BIN for FE_ATTRIB / GAMEPLAY
            try:
                attr_raw = arc.read_entry(arc.find(r"GLOBAL\ATTRIBUTES.BIN"))
                schema_db = VpakDatabase(attr_raw)
            except KeyError:
                schema_db = None
    else:
        raw = source.read_bytes()

    if schema_bin is not None:
        schema_db = VpakDatabase(schema_bin.read_bytes())

    return raw, schema_db


def _load_extra_names(names_path: Optional[Path]) -> Optional[List[str]]:
    if names_path is None:
        return None
    out: List[str] = []
    for line in names_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if " - " in s:
            s = s.split(" - ", 1)[1].strip()
        out.append(s)
    return out


# ---------------------------------------------------------------------------
# CLI Implementation (Subcommands + Backwards-Compatible Default Inspection)
# ---------------------------------------------------------------------------

SUBCOMMANDS = {"inspect", "classes", "nodes", "dump-node", "patch"}


def _cmd_inspect(args: argparse.Namespace) -> int:
    source = args.iso or args.in_file or args.source
    if source is None:
        print("ERROR: Specify a source .iso or .bin file.", file=sys.stderr)
        return 2
    raw, _ = _load_vpak_source(source, args.virtual_path)
    parsed = parse_vpak(raw)
    if args.json:
        print(json.dumps(parsed, indent=2))
        return 0

    src_label = args.virtual_path if source.suffix.lower() == ".iso" else "raw"
    print(f"VPAK container: {source} ({src_label})")
    print(f"Total size    : {parsed['total_size']:,} bytes | Vaults: {parsed['vault_count']}")
    for v in parsed["vaults"][:10]:
        chunk_str = ", ".join(f"{c['tag']}({c['size']:,}B)" for c in v["chunks"])
        print(f"\n  Vault [{v['index']}] {v['name']!r}")
        print(
            f"    BIN: off=0x{v['bin_offset']:x} size={v['bin_size']:,} | "
            f"VLT: off=0x{v['vlt_offset']:x} size={v['vlt_size']:,}"
        )
        print(f"    Chunks: {chunk_str}")
        if v["known_classes_detected"]:
            print(
                f"    Detected VLT Classes ({len(v['known_classes_detected'])}): "
                + ", ".join(v["known_classes_detected"])
            )
        if v["resolved_exports"]:
            print(
                f"    Resolved Exports ({len(v['resolved_exports'])}): "
                + ", ".join(e["class"] for e in v["resolved_exports"][:20])
            )
    if len(parsed["vaults"]) > 10:
        print(f"\n  ... ({len(parsed['vaults']) - 10} additional vaults omitted; use --json for full dump)")
    return 0


def _cmd_classes(args: argparse.Namespace) -> int:
    source = args.iso or args.in_file or args.source
    if source is None:
        print("ERROR: Specify a source .iso or .bin file (positional or --iso / --in).", file=sys.stderr)
        return 2
    raw, schema_db = _load_vpak_source(source, args.virtual_path, args.schema)
    db = VpakDatabase(raw, schema_source=schema_db, extra_names=_load_extra_names(args.names))
    rows = db.list_classes()
    if args.json:
        print(json.dumps({"vault_count": len(db.vaults), "classes": rows}, indent=2))
        return 0

    total_cols = sum(r["collections"] for r in rows)
    print(f"Classes in {source} ({args.virtual_path if source.suffix.lower() == '.iso' else 'raw'}): {len(rows)} classes, {total_cols} collections across {len(db.vaults)} vault(s)")
    print(f"  {'Class':26s} {'Hash':10s} {'Defs':>5s} {'Layout':>8s} {'Base':>5s} {'Collections':>11s}")
    for r in rows:
        print(
            f"  {r['class']:26s} {r['hash']:10s} {r['definitions']:5d} "
            f"{r['layout_size']:7d}B {r['layout_fields']:5d} {r['collections']:11d}"
        )
    return 0


def _cmd_nodes(args: argparse.Namespace) -> int:
    source = args.iso or args.in_file
    pos = args.pos
    if source is None:
        if len(pos) != 2:
            print("ERROR: Usage: nodes <source> <class>  OR  nodes <class> --iso <src.iso>", file=sys.stderr)
            return 2
        source = Path(pos[0])
        class_ref = pos[1]
    else:
        if len(pos) != 1:
            print("ERROR: Usage: nodes <class> --iso <src.iso>", file=sys.stderr)
            return 2
        class_ref = pos[0]

    raw, schema_db = _load_vpak_source(source, args.virtual_path, args.schema)
    db = VpakDatabase(raw, schema_source=schema_db, extra_names=_load_extra_names(args.names))
    cols = db.list_nodes(class_ref, vault_name=args.vault)

    if args.json:
        out = [
            {
                "vault": c.vault_name,
                "class": c.class_name,
                "name": c.name,
                "key": f"0x{c.key:08x}",
                "parent": c.parent_name,
                "parent_key": f"0x{c.parent_key:08x}" if c.parent_key else None,
                "layout_file_offset": f"0x{c.layout_file_offset:08x}" if c.layout_file_offset else None,
                "local_fields": len(c.fields),
            }
            for c in cols
        ]
        print(json.dumps(out, indent=2))
        return 0

    print(f"Nodes in class {class_ref!r}: {len(cols)} collection(s)")
    for c in cols:
        par = f" -> parent={c.parent_name}" if c.parent_name else ""
        lay = f" layout@0x{c.layout_file_offset:06x}" if c.layout_file_offset else ""
        vtag = f"[{c.vault_name}] " if len(db.vaults) > 1 else ""
        print(f"  {vtag}{c.name:32s} 0x{c.key:08x}{lay}  fields={len(c.fields):2d}{par}")
    return 0


def _cmd_dump_node(args: argparse.Namespace) -> int:
    source = args.iso or args.in_file
    pos = args.pos
    if source is None:
        if len(pos) != 3:
            print(
                "ERROR: Usage: dump-node <source> <class> <node>  OR  dump-node <class> <node> --iso <src.iso>",
                file=sys.stderr,
            )
            return 2
        source = Path(pos[0])
        class_ref, node_ref = pos[1], pos[2]
    else:
        if len(pos) != 2:
            print("ERROR: Usage: dump-node <class> <node> --iso <src.iso>", file=sys.stderr)
            return 2
        class_ref, node_ref = pos[0], pos[1]

    raw, schema_db = _load_vpak_source(source, args.virtual_path, args.schema)
    db = VpakDatabase(raw, schema_source=schema_db, extra_names=_load_extra_names(args.names))
    col = db.find_collection(class_ref, node_ref, vault_name=args.vault)
    fields = db.get_effective_fields(col, include_inherited=args.inherited)

    if args.json:
        payload = {
            "vault": col.vault_name,
            "class": col.class_name,
            "class_hash": f"0x{col.class_hash:08x}",
            "node": col.name,
            "node_key": f"0x{col.key:08x}",
            "parent": col.parent_name,
            "parent_key": f"0x{col.parent_key:08x}" if col.parent_key else None,
            "vlt_export_offset": f"0x{col.vlt_export_offset:08x}",
            "layout_file_offset": f"0x{col.layout_file_offset:08x}" if col.layout_file_offset else None,
            "fields": [fv.to_dict() for fv in sorted(fields.values(), key=lambda x: (x.file_offset, x.name))],
        }
        print(json.dumps(payload, indent=2))
        return 0

    print(
        f"Node {col.class_name}/{col.name} (key=0x{col.key:08x}, vault={col.vault_name!r}, "
        f"parent={col.parent_name or 'None'})"
    )
    if col.layout_file_offset is not None:
        print(f"  Layout block file offset: 0x{col.layout_file_offset:08x} | VLT export @ 0x{col.vlt_export_offset:08x}")
    else:
        print(f"  Layout block: None | VLT export @ 0x{col.vlt_export_offset:08x}")
    print(f"  Fields ({len(fields)}):")
    for fv in sorted(fields.values(), key=lambda x: (0 if x.inherited_from is None else 1, x.file_offset, x.name)):
        inh = f" [inherited:{fv.inherited_from}]" if fv.inherited_from else ""
        if fv.is_array:
            print(
                f"    {fv.name:28s} {fv.type_name}[{fv.array_count}/{fv.array_alloc}] "
                f"({fv.storage} @ 0x{fv.file_offset:08x}){inh}"
            )
            if fv.element_offsets:
                for idx, (eoff, eval_item) in enumerate(zip(fv.element_offsets, fv.value)):
                    print(f"      [{idx}] @ 0x{eoff:08x}: {eval_item}")
        else:
            print(
                f"    {fv.name:28s} {fv.type_name:24s} ({fv.storage:10s} @ 0x{fv.file_offset:08x}) = {fv.value}{inh}"
            )
    return 0


def _cmd_patch(args: argparse.Namespace) -> int:
    source = args.iso or args.in_file
    if source is None:
        print("ERROR: Specify --iso <src.iso> or --in <extracted.bin>.", file=sys.stderr)
        return 2

    raw, schema_db = _load_vpak_source(source, args.virtual_path, args.schema)
    recipes = [json.loads(rp.read_text(encoding="utf-8")) for rp in args.recipe]

    # Optional target virtual_path sanity check
    norm_vp = args.virtual_path.replace("/", "\\").upper()
    for r in recipes:
        rvp = r.get("target", {}).get("virtual_path")
        if rvp and rvp.replace("/", "\\").upper() != norm_vp:
            print(
                f"WARNING: Recipe {r.get('id', '?')!r} targets {rvp!r}, CLI --virtual-path is {args.virtual_path!r}",
                file=sys.stderr,
            )

    patched_bytes, audit = apply_vlt_recipes(raw, recipes, schema_source=schema_db)
    assert len(patched_bytes) == len(raw), "In-place VLT patch must preserve exact byte size!"

    memfile_audit = None
    if not args.dry_run:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_bytes(patched_bytes)

        if args.memfile_out is not None:
            if source.suffix.lower() != ".iso":
                raise ValueError("--memfile-out requires --iso <src.iso> to read GLOBAL\\GLOBALMEMORYFILE.BIN")
            arc = PS2NFSMWArchive(source)
            mf_raw = arc.read_entry(arc.find(r"GLOBAL\GLOBALMEMORYFILE.BIN"))
            mf_patched, memfile_audit = patch_memory_file_entry(mf_raw, args.virtual_path, patched_bytes)
            args.memfile_out.parent.mkdir(parents=True, exist_ok=True)
            args.memfile_out.write_bytes(mf_patched)

    report = {
        "source": str(source),
        "virtual_path": args.virtual_path,
        "output": str(args.out) if not args.dry_run else None,
        "dry_run": args.dry_run,
        "byte_size": len(patched_bytes),
        "recipes_applied": [r.get("id", str(p)) for r, p in zip(recipes, args.recipe)],
        "patch_count": len(audit),
        "writes": audit,
        "memfile_update": memfile_audit,
    }
    if args.report_json is not None:
        args.report_json.parent.mkdir(parents=True, exist_ok=True)
        args.report_json.write_text(json.dumps(report, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    mode = "DRY-RUN VERIFIED" if args.dry_run else "PATCHED"
    print(f"[{mode}] {len(audit)} verified VLT field write(s) across {len(recipes)} recipe(s)")
    for w in audit:
        print(
            f"  {w['file_offset']}  {w['target']:44s}  {w['orig_value']!r} -> {w['new_value']!r}  "
            f"({w['orig_hex']} -> {w['new_hex']})"
        )
    if not args.dry_run:
        print(f"Wrote patched VPAK ({len(patched_bytes):,} B): {args.out}")
        if memfile_audit:
            print(
                f"Wrote synced MemoryFile ({args.memfile_out}): "
                f"updated {memfile_audit['virtual_path']} @ {memfile_audit['embedded_offset']}"
            )
        elif norm_vp == r"GLOBAL\ATTRIBUTES.BIN":
            print(
                "NOTE: On PS2 SLUS-21351, GLOBAL\\ATTRIBUTES.BIN is also embedded byte-identically at "
                "0x380 inside GLOBAL\\GLOBALMEMORYFILE.BIN. Pass --memfile-out to emit a synced "
                "GLOBALMEMORYFILE.BIN when injecting into a modded ISO."
            )
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="PS2 NFSMW VPAK/VLT parser, inspector & verified JSON-recipe patcher"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # inspect
    p_ins = sub.add_parser("inspect", help="Inspect VPAK header, chunks & class overview")
    p_ins.add_argument("source", nargs="?", type=Path, default=None, help="Path to PS2 .iso or extracted .bin")
    p_ins.add_argument("--iso", type=Path, default=None, help="Path to PS2 .iso (read-only)")
    p_ins.add_argument("--in", dest="in_file", type=Path, default=None, help="Path to extracted VPAK .bin")
    p_ins.add_argument("--virtual-path", default=r"GLOBAL\ATTRIBUTES.BIN")
    p_ins.add_argument("--json", action="store_true")
    p_ins.set_defaults(func=_cmd_inspect)

    # classes
    p_cls = sub.add_parser("classes", help="List all VLT classes & collection counts")
    p_cls.add_argument("source", nargs="?", type=Path, default=None, help="Path to PS2 .iso or extracted .bin")
    p_cls.add_argument("--iso", type=Path, default=None, help="Path to PS2 .iso (read-only)")
    p_cls.add_argument("--in", dest="in_file", type=Path, default=None, help="Path to extracted VPAK .bin")
    p_cls.add_argument("--virtual-path", default=r"GLOBAL\ATTRIBUTES.BIN")
    p_cls.add_argument("--schema", type=Path, default=None, help="Optional ATTRIBUTES.BIN schema for FE_ATTRIB/GAMEPLAY")
    p_cls.add_argument("--names", type=Path, default=None, help="Optional extra VLT name wordlist")
    p_cls.add_argument("--json", action="store_true")
    p_cls.set_defaults(func=_cmd_classes)

    # nodes
    p_nod = sub.add_parser("nodes", help="List all collection nodes belonging to a class")
    p_nod.add_argument("pos", nargs="+", help="<source> <class>  OR  <class> (with --iso/--in)")
    p_nod.add_argument("--iso", type=Path, default=None, help="Path to PS2 .iso (read-only)")
    p_nod.add_argument("--in", dest="in_file", type=Path, default=None, help="Path to extracted VPAK .bin")
    p_nod.add_argument("--virtual-path", default=r"GLOBAL\ATTRIBUTES.BIN")
    p_nod.add_argument("--vault", default=None, help="Optional vault name filter (useful for GAMEPLAY.BIN)")
    p_nod.add_argument("--schema", type=Path, default=None)
    p_nod.add_argument("--names", type=Path, default=None)
    p_nod.add_argument("--json", action="store_true")
    p_nod.set_defaults(func=_cmd_nodes)

    # dump-node
    p_dmp = sub.add_parser("dump-node", help="Decode fields & exact byte offsets of a VLT collection node")
    p_dmp.add_argument("pos", nargs="+", help="<source> <class> <node>  OR  <class> <node> (with --iso/--in)")
    p_dmp.add_argument("--iso", type=Path, default=None, help="Path to PS2 .iso (read-only)")
    p_dmp.add_argument("--in", dest="in_file", type=Path, default=None, help="Path to extracted VPAK .bin")
    p_dmp.add_argument("--virtual-path", default=r"GLOBAL\ATTRIBUTES.BIN")
    p_dmp.add_argument("--vault", default=None, help="Optional vault name filter")
    p_dmp.add_argument("--inherited", action="store_true", help="Include inherited fields from parent chain")
    p_dmp.add_argument("--schema", type=Path, default=None)
    p_dmp.add_argument("--names", type=Path, default=None)
    p_dmp.add_argument("--json", action="store_true")
    p_dmp.set_defaults(func=_cmd_dump_node)

    # patch
    p_pat = sub.add_parser("patch", help="Apply verified JSON override recipe(s) to a VPAK database")
    p_pat.add_argument("--iso", type=Path, default=None, help="Path to clean PS2 .iso (strictly read-only)")
    p_pat.add_argument("--in", dest="in_file", type=Path, default=None, help="Path to extracted VPAK .bin")
    p_pat.add_argument("--virtual-path", default=r"GLOBAL\ATTRIBUTES.BIN")
    p_pat.add_argument("--recipe", type=Path, action="append", required=True, help="Recipe JSON path (repeatable)")
    p_pat.add_argument("--out", type=Path, required=True, help="Output path for patched .bin")
    p_pat.add_argument(
        "--memfile-out",
        type=Path,
        default=None,
        help="Optional output path for synced GLOBALMEMORYFILE.BIN (when reading from --iso)",
    )
    p_pat.add_argument("--schema", type=Path, default=None)
    p_pat.add_argument("--dry-run", action="store_true", help="Verify all patches without writing output")
    p_pat.add_argument("--report-json", type=Path, default=None, help="Write JSON audit report")
    p_pat.add_argument("--json", action="store_true")
    p_pat.set_defaults(func=_cmd_patch)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    # Backwards compatibility: `vlt_ps2.py <source> [--virtual-path ...] [--json]`
    if raw_argv and raw_argv[0] not in SUBCOMMANDS and not raw_argv[0].startswith("-"):
        raw_argv = ["inspect"] + raw_argv

    parser = build_arg_parser()
    args = parser.parse_args(raw_argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
