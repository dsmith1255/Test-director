-- WIMNA-only experimental overlay. It never changes or redistributes FH26 files.
-- A physics-worker command is not proof that its physical effect was observed.
local appID = 'WimnaTestDirector'
local parameters = [[{"aero_force":"speed_squared_proxy","air_pressure_pa_assumed":101325,"ambient_fallback_c":20.0,"cooling_model_multiplier":1.10,"delta_cd_a_m2":0.012,"delta_downforce_area_m2":0.030,"max_downforce_n":250.0,"max_drag_n":100.0,"max_dt_s":0.015,"max_speed_m_s":100.0,"nominal_step_s":0.003,"variant":"plan_c","version":"wimna-prototype-v1"}]]
local parameterHash = '4cafb3a4cf76afe426126b660996fd29777a58f031322c4ccc9a2ef974697cd9'
local stamped = false
local workerAttempted = false
local openAttempted = false

local function storeEffect(value)
  pcall(ac.store, 'wimna.prototype.effect.v1', value)
end

local function stamp()
  ac.store('wimna.session_mode.v1', 'wimna-ai-plan-c')
  ac.store('wimna.prototype.variant.v1', 'plan_c')
  ac.store('wimna.prototype.parameter_hash.v1', parameterHash)
  ac.store('wimna.prototype.parameters.v1', parameters)
  ac.store('wimna.prototype.effect.v1', 'pending')
  ac.store('wimna.prototype.permission.v1', 0)
  -- CSP marks physics.addForce laps invalid. AI control can also invalidate laps.
  ac.store('wimna.prototype.native_invalid_expected.v1', 1)
end

local function startWorker()
  workerAttempted = true
  local checked, allowed = pcall(physics.allowed)
  if not checked or allowed ~= true then
    storeEffect('permission_unavailable')
    return
  end
  pcall(ac.store, 'wimna.prototype.permission.v1', 1)
  local workerError = false
  local started = pcall(physics.startPhysicsWorker, 'aero_worker', {
    deltaCdA = 0.012,
    deltaDownforceArea = 0.030,
    pressurePa = 101325,
    ambientFallbackC = 20.0,
    maxDragN = 100.0,
    maxDownforceN = 250.0,
    maxDtS = 0.015,
    maxSpeedMS = 100.0,
    nominalStepS = 0.003
  }, function(err)
    if err then
      workerError = true
      storeEffect('worker_start_failed')
    end
  end)
  storeEffect(started and not workerError and 'worker_requested_effect_unverified'
    or 'worker_start_failed')
end

function script.update(dt)
  if not stamped then stamped = pcall(stamp) end
  if stamped and not workerAttempted then startWorker() end
  if openAttempted then return end
  local checked, running = pcall(ac.isLuaAppRunning, appID)
  if checked and running then
    openAttempted = true
    pcall(ac.setAppOpen, appID)
  end
end
