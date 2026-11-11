-- Correspondence between the REN Data Hub components (table ren_generation)
-- and the ENTSO-E variables for Portugal (table entsoe_timeseries).
--
-- status:
--   compared            ENTSO-E variable(s) summed into the REN component
--   no_entsoe_category  the REN component has no ENTSO-E category for PT; taken as 0 on the ENTSO-E side
--   excluded_zero       ENTSO-E PT category that is 0 (or absent) on the days compared; excluded on purpose.
--                       tests/test_comparison.py fails if any of them becomes non-zero, so the mapping gets reviewed
--   not_compared        small units' own consumption with no REN counterpart
--
-- sign: +1 / -1, applied to the ENTSO-E value (used for the net interconnection balance)
-- is_generation: the component is part of total generation
CREATE OR REPLACE TEMP TABLE component_map AS
SELECT * FROM (VALUES
  -- country, entsoe_variable,                   component,           sign, is_generation, status,               note
  ('PT', 'generation_hydro_run_of_river',        'hydro',             1, true,  'compared',           NULL),
  ('PT', 'generation_hydro_reservoir',           'hydro',             1, true,  'compared',           NULL),
  ('PT', 'generation_hydro_pumped_storage',      'hydro',             1, true,  'compared',           'REN hydro includes pumped-storage turbine generation (SOURCES.md)'),
  ('PT', 'generation_wind_onshore',              'wind',              1, true,  'compared',           NULL),
  ('PT', 'generation_wind_offshore',             'wind',              1, true,  'compared',           NULL),
  ('PT', 'generation_solar',                     'solar',             1, true,  'compared',           NULL),
  ('PT', 'generation_biomass',                   'biomass',           1, true,  'compared',           NULL),
  ('PT', 'generation_fossil_gas',                'natural_gas',       1, true,  'compared',           'REN natural_gas is CCGT only; ENTSO-E fossil_gas covers all gas plants (cogeneration included)'),
  ('PT', 'generation_fossil_hard_coal',          'coal',              1, true,  'compared',           NULL),
  ('PT', 'generation_other',                     'other_thermal',     1, true,  'compared',           'Empirical match; ENTSO-E does not say what "other" contains'),
  (NULL, NULL,                                   'wave',              1, true,  'no_entsoe_category', 'No marine category for PT in ENTSO-E'),
  (NULL, NULL,                                   'battery_discharge', 1, true,  'no_entsoe_category', 'No energy-storage generation reported for PT in ENTSO-E'),
  ('PT', 'consumption_hydro_pumped_storage',     'pumping',           1, false, 'compared',           'ENTSO-E is 0.8-2.2% higher; main hypothesis: plants'' auxiliary consumption counted in one source only (SOURCES.md)'),
  ('PT', 'load_actual',                          'consumption',       1, false, 'compared',           NULL),
  ('ES', 'physical_flow_to_PT',                  'net_interconnection', 1, false, 'compared',         'Net imports = flow ES->PT minus flow PT->ES'),
  ('PT', 'physical_flow_to_ES',                  'net_interconnection', -1, false, 'compared',        'Net imports = flow ES->PT minus flow PT->ES'),
  -- Excluded on purpose: exist as ENTSO-E categories for PT but are 0 on the 4 days compared
  -- (the API returns no series for them at all, which we treat as 0).
  ('PT', 'generation_fossil_oil',                NULL,                1, true,  'excluded_zero',      'Zero on the days compared'),
  ('PT', 'generation_waste',                     NULL,                1, true,  'excluded_zero',      'Zero on the days compared'),
  ('PT', 'generation_other_renewable',           NULL,                1, true,  'excluded_zero',      'Zero on the days compared'),
  ('PT', 'generation_energy_storage',            NULL,                1, true,  'excluded_zero',      'Zero on the days compared'),
  ('PT', 'consumption_energy_storage',           NULL,                1, false, 'excluded_zero',      'Zero on the days compared'),
  -- Not compared: units' own consumption, <= 5.5 MW on average, no REN counterpart
  ('PT', 'consumption_fossil_gas',               NULL,                1, false, 'not_compared',       'Small own consumption, no REN counterpart'),
  ('PT', 'consumption_fossil_hard_coal',         NULL,                1, false, 'not_compared',       'Small own consumption, no REN counterpart'),
  ('PT', 'consumption_hydro_reservoir',          NULL,                1, false, 'not_compared',       'Small own consumption, no REN counterpart'),
  ('PT', 'consumption_hydro_run_of_river',       NULL,                1, false, 'not_compared',       'Small own consumption, no REN counterpart')
) AS t(entsoe_country, entsoe_variable, component, sign, is_generation, status, note);
