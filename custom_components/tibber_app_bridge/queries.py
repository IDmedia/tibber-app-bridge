"""GraphQL documents used by the Tibber Android app (read-only, trimmed).

Operation names match the app so the traffic looks like the app's own.

Read-only by design: only `query` (HTTP) and `subscription` (websocket)
operations exist here, and `operation_name` refuses anything else, so no
code path can change settings in a Tibber account.
"""

from __future__ import annotations

import re

_OPERATION_RE = re.compile(r"^\s*(query|subscription)\s+([A-Za-z_][A-Za-z0-9_]*)")
_MUTATION_RE = re.compile(r"\bmutation\b")


class ReadOnlyViolation(ValueError):
    """A GraphQL document that could change data was about to be sent."""


def operation_name(document: str, kind: str) -> str:
    """Name of a read-only operation; raise if it isn't `kind` or could mutate.

    `kind` is "query" for HTTP requests and "subscription" for websockets.
    """
    match = _OPERATION_RE.match(document)
    if not match or match.group(1) != kind or _MUTATION_RE.search(document):
        raise ReadOnlyViolation(f"Refusing to send a non-{kind} GraphQL document")
    return match.group(2)


_SETTING = "fragment setting on Setting { key value valueType isReadOnly }"
_LAYOUT = (
    "fragment layoutFields on SettingLayoutItem { uid type title valueText settingKey } "
    "fragment layout on SettingLayoutItem { ...layoutFields childItems { ...layoutFields "
    "childItems { ...layoutFields childItems { ...layoutFields } } } }"
)

GET_HOMES = (
    """
query GetHomes { me { id firstName homes { id title type avatar timeZone
  hasSmartMeterCapabilities hasSignedEnergyDeal meteringPointIdFormatted
  currentMeter { meterNo }
  address { addressText city postalCode country latitude longitude }
  settings { ...setting } } } }
"""
    + _SETTING
)

GET_HOME_GIZMOS = """
query GetHomeGizmos($homeId: String!) { me { home(id: $homeId) { gizmos {
  __typename ... on Gizmo { id title type isHidden }
  ... on GizmoGroup { id title gizmos { id title type isHidden } } } } } }
"""

PULSE_QUERY = (
    """
query PulseQuery($homeId: String!, $deviceId: String) { me { home(id: $homeId) {
  pulse(id: $deviceId) { id name type shortName isAlive hasPhaseCurrents
    peakControlStatus estimatedMeasurementsPerMinute
    error { title description } warning { title description }
    settings2 { ...setting } settingsLayout { ...layout } } } } }
"""
    + _SETTING
    + " "
    + _LAYOUT
)

_MEASUREMENT = (
    "fragment m on PulseMeasurement { timestamp power powerProduction "
    "currentPhase1 currentPhase2 currentPhase3 voltagePhase1 voltagePhase2 voltagePhase3 }"
)

# ~10 s resolution, used for the "live" feed (last few minutes).
PULSE_MEASUREMENTS_CACHE = (
    """
query PulseMeasurementHistoryCache($homeId: String!, $deviceId: String!, $from: String!, $to: String!) {
  me { home(id: $homeId) { pulseMeasurements(id: $deviceId, from: $from, to: $to) { ...m } } } }
"""
    + _MEASUREMENT
)

# 1 min averages, used for today's min/max and the not-yet-billed part of the day.
PULSE_MEASUREMENTS_STORAGE = (
    """
query PulseMeasurementHistoryStorage($homeId: String!, $deviceId: String!, $from: String!, $to: String!) {
  me { home(id: $homeId) { pulseMeasurementHistory(id: $deviceId, from: $from, to: $to) { ...m } } } }
"""
    + _MEASUREMENT
)

PEAK_CONTROL = """
query GetPeakControlData($homeId: String!) { me { home(id: $homeId) { peakControlData {
  isActive isAlarmActive hasRealTimeDevice consumptionLimit lowerBound recommendedValue upperBound } } } }
"""

FUSE_LOAD_EXCESS = """
query GetFuseLoadExcess($homeId: String!) { me { home(id: $homeId) { fuseLoadExcess {
  history { items { startedAt endedAt currentPhase1 currentPhase2 currentPhase3 mainFuseSize } } } } } }
"""

_PRICE_PERIOD = (
    "fragment p on PriceRating { minEnergy maxEnergy minTotal maxTotal currency "
    "entries { time energy total compensatedTotal gridPrice } }"
)

PRICE_RATING = (
    """
query PriceRatingQuery($homeId: String!) { me { home(id: $homeId) { subscription { priceRating {
  isAvailable nemoPriceProviderName defaultResolution
  gridPriceToggler { gridCompanyName gridCompanyProvidesData }
  compensatedPriceToggler { title }
  thresholdPercentages { high low highBreakpoint }
  quarterHourly { ...p } hourly { ...p } daily { ...p } monthly { ...p } } } } } }
"""
    + _PRICE_PERIOD
)

PRICE_PERFORMANCE_GIZMO = """
query PricePerformanceGizmoData($homeId: String!) { me { home(id: $homeId) {
  pricePerformanceGizmoData { from value currency } } } }
"""

CONSUMPTION_GIZMO = """
query ConsumptionGizmoData($homeId: String!) { me { home(id: $homeId) {
  consumptionGizmoData { from cost consumption currency } } } }
"""

CONSUMPTION_WITH_CHART = """
query ConsumptionAnalysisItemsWithChartQuery($homeId: String!, $from: String!, $to: String!, $resolution: Resolution!, $useDemoData: Boolean!) {
  me { home(id: $homeId) { consumptionAnalysisItems(from: $from, to: $to, resolution: $resolution, useDemoData: $useDemoData) {
    from to description resolution currency
    netConsumption { cost consumption } selfConsumption { cost consumption }
    estimatedTotalConsumption { cost consumption }
    chart { items { from to totalConsumption netConsumption selfConsumption totalCost price qualityTag } } } } } }
"""

PRICE_PERFORMANCE_ITEMS = """
query PricePerformanceAnalysisItems($homeId: String!, $from: String!, $to: String!, $resolution: Resolution!) {
  me { home(id: $homeId) { pricePerformanceAnalysisItems(from: $from, to: $to, resolution: $resolution) {
    from to homeAveragePrice currency priceArea
    comparison { minAveragePrice midAveragePrice maxAveragePrice }
    disaggregation { alwaysOn { averagePrice consumption } behavior { averagePrice consumption }
      charging { averagePrice consumption } heating { averagePrice consumption } }
    highlights { __typename
      ... on PricePerformanceChargingHighlight { currency details { absoluteValue type } }
      ... on PricePerformanceReductionHighlight { total currency details { absoluteValue type } } } } } } }
"""

COMPARISON_ITEMS = """
query ComparisonAnalysisItemsQuery($homeId: String!, $from: String!, $to: String!, $resolution: Resolution!, $useDemoData: Boolean!) {
  me { home(id: $homeId) { comparisonAnalysisItems(from: $from, to: $to, resolution: $resolution, useDemoData: $useDemoData) {
    from to resolution homeEfficency homeEfficencyDescription
    home { cost consumption } average { cost consumption } efficient { cost consumption } } } } }
"""

COST_DISAGGREGATION = """
query CostDisaggregationItemsQuery($homeId: String!, $from: String!, $to: String!, $resolution: Resolution!, $useDemoData: Boolean!) {
  me { home(id: $homeId) { costDisaggregationAnalysisItems(from: $from, to: $to, resolution: $resolution, useDemoData: $useDemoData) {
    from to title total totalDescription items { title type consumption cost percent description } } } } }
"""

GRID_REWARDS_HISTORY = """
query GetGridRewardsHistory($homeId: String!) { me { home(id: $homeId) {
  gridRewardsHistory { resolutions valuesFrom valuesTo } } } }
"""

GRID_REWARDS_PERIOD = """
query GetGridRewardsHistoryPeriod($homeId: String!, $from: String!, $to: String!, $resolution: Resolution!) {
  me { home(id: $homeId) { gridRewardsHistoryPeriod(from: $from, to: $to, resolution: $resolution) {
    from to batteryRewards vehicleRewards totalReward currency
    chart { items { from to batteryRewards vehicleRewards totalReward } } } } } }
"""

GRID_REWARDS_INFO = """
query GetGridRewardsInfo($homeId: String!) { me { home(id: $homeId) { gridRewardsInfo {
  chargingPricePerformanceLastMonth { title from to currency averagePrice averagePriceMissingReason } } } } }
"""

CHARGER_MONTHLY = """
query GetChargerConsumptionMonthly($homeId: String!, $chargerId: String!, $from: String, $to: String) {
  me { home(id: $homeId) { evChargerConsumptionMonths(id: $chargerId, from: $from, to: $to) {
    year month consumption energyCost currency } } } }
"""

CHARGER_CONSUMPTION = """
query GetChargerConsumption($homeId: String!, $chargerId: String!, $from: String, $to: String, $resolution: String) {
  me { home(id: $homeId) { evChargerConsumption(id: $chargerId, from: $from, to: $to, resolution: $resolution) {
    from consumption energyCost } } } }
"""

MY_VEHICLES = (
    """
query GetMyVehicles { me { myVehicles { vehicles { id title description isPaired
  detailsScreen { settings { ...setting } } } } } }
"""
    + _SETTING
)

# Subscriptions (graphql-transport-ws). Same documents as the app, minus the
# signed image URLs. `gridRewardsSubscription` sends one snapshot and completes;
# `gridRewardStateTransitionsSubscription` stays open and pushes changes.
_GRID_REWARD = (
    "fragment gridRewardState on GridRewardState { __typename "
    "... on GridRewardAvailable { kind } "
    "... on GridRewardUnavailable { reasons sheetTitle sheetDescription } "
    "... on GridRewardDelivering { reason } } "
    "fragment gridRewardVehicle on GridRewardVehicle { kind vehicleId shortName make "
    "isPluggedIn isSmartChargingEnabled state { ...gridRewardState } } "
    "fragment gridRewardBattery on GridRewardBattery { kind batteryId shortName make "
    "isSmartModeEnabled state { ...gridRewardState } } "
    "fragment gridReward on GridReward { homeId state { ...gridRewardState } "
    "rewardCurrency rewardCurrentMonth rewardAllTime flexDevices { __typename "
    "... on GridRewardVehicle { ...gridRewardVehicle } "
    "... on GridRewardBattery { ...gridRewardBattery } } }"
)

GRID_REWARDS_SUBSCRIPTION = (
    "subscription gridRewardsSubscription($homeId: String!) { "
    "gridRewardStatus(homeId: $homeId) { ...gridReward } } " + _GRID_REWARD
)

GRID_REWARD_TRANSITIONS_SUBSCRIPTION = (
    "subscription gridRewardStateTransitionsSubscription($homeId: String!) { "
    "gridRewardStateTransitions(homeId: $homeId) { ...gridReward } } " + _GRID_REWARD
)

SMART_CHARGING_SETUP = """
query GetSmartChargingSetupInfo { me { smartChargingSetupInfo {
  hasAnyEnergyDeal hasAnyPulse hasAnyVehicle hasAnyCharger } } }
"""

# Fragments returned for chargers and vehicles (seen in the app's responses).
# They arrive via the vehicleChargerSubscription and vehicleStateSubscription
# subscriptions below; images,
# action buttons and settings screens are left out on purpose.
_SESSION = (
    "fragment chargeSession on VehicleChargeSession { __typename "
    "... on ManualChargeSession { kind manualChargeSessionState { __typename "
    "... on ManualChargeSessionStateCharging { kind currentChargePower } "
    "... on ManualChargeSessionStateNotCharging { kind notChargingReason } "
    "... on VehicleManualChargeSessionStateError { kind errorReason } } } "
    "... on SmartChargeSession { kind smartChargingSessionState { __typename "
    "... on SmartChargingSessionStateWaitingForInitialSchedule { kind } "
    "... on SmartChargingSessionStateCharging { kind chargingReason currentChargePower } "
    "... on SmartChargingSessionStateNotCharging { kind notChargingReason } "
    "... on SmartChargingSessionStateCompletedCharging { kind completed } "
    "... on VehicleSmartChargingSessionStateError { kind errorReason } } } }"
)

VEHICLE_CHARGER_FRAGMENT = (
    "fragment vehicleCharger on VehicleCharger { id name status { title description } "
    "lastSeen isAlive chargingStatus connectivity { __typename "
    "... on VehicleChargerConnectivityAvailable { kind } "
    "... on VehicleChargerConnectivityUnavailable { kind unavailableReason } "
    "... on VehicleChargerConnectivityDenied { kind deniedReason } "
    "... on VehicleChargerConnectivityUnsupported { kind } } "
    "userSettings { key value } vehicles { id name isPreferred } "
    "chargeSession { ...chargeSession } } " + _SESSION
)

VEHICLE_FRAGMENT = (
    "fragment vehicle on Vehicle { id name isAlive chargingStatus smartChargingStatus "
    "isAtHome battery { level estimatedRange canReadLevel } status { title description } "
    "charging { summary { energyConsumed totalBilled totalNonBilled total gridRewards "
    "averageChargePower averagePrice priceCap } currency sessionStartedAt "
    "targetedStateOfCharge targetedDepartureTime chargerId "
    "days { date targetSoc periods { startAt stopAt fromStateOfCharge toStateOfCharge "
    "energy cost type gridReward averagePrice } } session { ...chargeSession } } "
    "isGridRewardsEnabled } " + _SESSION
)

VEHICLE_STATE_SUBSCRIPTION = (
    "subscription vehicleStateSubscription($vehicleId: String!) { "
    "vehicleState(vehicleId: $vehicleId) { ...vehicle } } " + VEHICLE_FRAGMENT
)

VEHICLE_CHARGER_SUBSCRIPTION = (
    "subscription vehicleChargerSubscription($chargerId: String!) { "
    "vehicleChargerState(chargerId: $chargerId) { ...vehicleCharger } } " + VEHICLE_CHARGER_FRAGMENT
)

# Live Pulse feed. Replays ~10 min of samples on subscribe, then one per ~10 s.
PULSE_SUBSCRIPTION = (
    "subscription pulseSubscription($deviceId: String!) { "
    "liveMeasurement(deviceId: $deviceId) { ...RealTimeMeasurement } } "
    "fragment RealTimeMeasurement on PulseMeasurement { timestamp power powerProduction "
    "minPower minPowerTimestamp averagePower maxPower maxPowerTimestamp "
    "minPowerProduction maxPowerProduction estimatedAccumulatedConsumptionCurrentHour "
    "accumulatedConsumption accumulatedCost accumulatedConsumptionCurrentHour "
    "accumulatedProduction accumulatedReward accumulatedProductionCurrentHour "
    "peakControlConsumptionState effectiveHourlyConsumptionLimit currency "
    "currentPhase1 currentPhase2 currentPhase3 voltagePhase1 voltagePhase2 voltagePhase3 }"
)
