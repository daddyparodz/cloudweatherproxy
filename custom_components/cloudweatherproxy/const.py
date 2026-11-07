"""Constants for the Cloud Weather Proxy."""

from dataclasses import replace
from typing import Final

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    DEGREE,
    LIGHT_LUX,
    PERCENTAGE,
    UV_INDEX,
    UnitOfIrradiance,
    UnitOfPrecipitationDepth,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
    UnitOfVolumetricFlux,
)

DOMAIN = "cloudweatherproxy"

UNIT_DESCRIPTION_MAPPING: Final = {
    UnitOfPressure.HPA: SensorEntityDescription(
        key="PRESSURE_HPA",
        device_class=SensorDeviceClass.PRESSURE,
        native_unit_of_measurement=UnitOfPressure.HPA,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
    ),
    UnitOfTemperature.CELSIUS: SensorEntityDescription(
        key="TEMPERATURE_C",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
    ),
    UnitOfPrecipitationDepth.MILLIMETERS: SensorEntityDescription(
        key="RAIN_MM",
        device_class=SensorDeviceClass.PRECIPITATION,
        native_unit_of_measurement=UnitOfPrecipitationDepth.MILLIMETERS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
    ),
    UnitOfSpeed.METERS_PER_SECOND: SensorEntityDescription(
        key="WIND_SPEED_MS",
        device_class=SensorDeviceClass.WIND_SPEED,
        native_unit_of_measurement=UnitOfSpeed.METERS_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
    ),
    UnitOfIrradiance.WATTS_PER_SQUARE_METER: SensorEntityDescription(
        key="SOLAR_RADIATION_W_M2",
        device_class=SensorDeviceClass.IRRADIANCE,
        native_unit_of_measurement=UnitOfIrradiance.WATTS_PER_SQUARE_METER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
    ),
    LIGHT_LUX: SensorEntityDescription(
        key="LIGHT_LUX",
        device_class=SensorDeviceClass.ILLUMINANCE,
        native_unit_of_measurement=LIGHT_LUX,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
    ),
    UnitOfVolumetricFlux.MILLIMETERS_PER_HOUR: SensorEntityDescription(
        key="RAIN_RATE_MM",
        device_class=SensorDeviceClass.PRECIPITATION_INTENSITY,
        native_unit_of_measurement=UnitOfVolumetricFlux.MILLIMETERS_PER_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
    ),
    DEGREE: SensorEntityDescription(
        key="WIND_DIRECTION",
        native_unit_of_measurement=DEGREE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    PERCENTAGE: SensorEntityDescription(
        key="HUMIDITY",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
    ),
    UV_INDEX: SensorEntityDescription(
        key="UV_INDEX",
        state_class=SensorStateClass.MEASUREMENT,
    ),
}

# Semantic quantities own metadata; incoming units never choose a device class.
QUANTITY_DESCRIPTIONS: Final = {}
for _names, _unit in (
    ("temperature temperature2 indoortemperature dewpoint dewpointindoor windchill heatindex heatindexindoor temphumiditywind", UnitOfTemperature.CELSIUS),
    ("humidity humidity2 indoorhumidity", PERCENTAGE),
    ("barometer absbarometer", UnitOfPressure.HPA),
    ("windspeed windgustspeed windspeedavg windgustspeed10m", UnitOfSpeed.METERS_PER_SECOND),
    ("winddirection windgustdirection winddirectionavg windgustdirection10m", DEGREE),
    ("uv", UV_INDEX),
    ("irradiance", UnitOfIrradiance.WATTS_PER_SQUARE_METER),
    ("rain_intensity", UnitOfVolumetricFlux.MILLIMETERS_PER_HOUR),
    ("wunderground_dailyrain wunderground_weeklyrain wunderground_monthlyrain weathercloud_dailyrain", UnitOfPrecipitationDepth.MILLIMETERS),
):
    for _name in _names.split():
        QUANTITY_DESCRIPTIONS[_name] = replace(UNIT_DESCRIPTION_MAPPING[_unit], key=_name)
QUANTITY_DESCRIPTIONS["rain_1h"] = replace(
    UNIT_DESCRIPTION_MAPPING[UnitOfPrecipitationDepth.MILLIMETERS],
    key="rain_1h", state_class=SensorStateClass.MEASUREMENT,
)

CONF_WUNDERGROUND_PROXY: Final = "weatherunderground_proxy"
CONF_WEATHERCLOUD_PROXY: Final = "weathercloud_proxy"
CONF_DNS_SERVERS: Final = "dns_servers"
