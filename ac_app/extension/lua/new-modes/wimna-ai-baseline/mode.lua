-- Paired reference mode. The existing FH26 physics files remain untouched.
local appID = 'WimnaTestDirector'
local parameters = [[{"aero_force":null,"cooling_model_multiplier":1.0,"effect":"none","variant":"baseline","version":"wimna-prototype-v1"}]]
local parameterHash = '1b0fd209d1c46087bdbf3f8ff0ac6fe30472ae7938b29d662371772e0496bb12'
local stamped = false
local permissionProbed = false
local openAttempted = false
local diagnosticWritten = false

local function stamp()
  ac.store('wimna.session_mode.v1', 'wimna-ai-baseline')
  ac.store('wimna.prototype.variant.v1', 'baseline')
  ac.store('wimna.prototype.parameter_hash.v1', parameterHash)
  ac.store('wimna.prototype.parameters.v1', parameters)
  ac.store('wimna.prototype.effect.v1', 'none')
  ac.store('wimna.prototype.permission.v1', 0)
  ac.store('wimna.prototype.native_invalid_expected.v1', 0)
  ac.store('wimna.prototype.force_call_pairs.v1', 0)
  ac.store('wimna.prototype.last_drag_n.v1', nil)
  ac.store('wimna.prototype.last_downforce_n.v1', nil)
  ac.store('wimna.prototype.last_speed_m_s.v1', nil)
  ac.store('wimna.prototype.worker_telemetry_status.v1', 'not_applicable')
end

function script.update(dt)
  if not stamped then
    local ok, err = pcall(stamp)
    stamped = ok
    if not diagnosticWritten then
      diagnosticWritten = true
      local readOk, readback = pcall(ac.load, 'wimna.session_mode.v1')
      pcall(ac.log, '[WIMNA baseline] update entered; store_ok=' .. tostring(ok) ..
        '; store_error=' .. tostring(err) .. '; same_context_load_ok=' .. tostring(readOk) ..
        '; same_context_mode=' .. tostring(readback))
    end
  end
  if stamped and not permissionProbed then
    permissionProbed = true
    local checked, allowed = pcall(physics.allowed)
    pcall(ac.store, 'wimna.prototype.permission.v1', checked and allowed == true and 1 or 0)
  end
  if openAttempted then return end
  local checked, running = pcall(ac.isLuaAppRunning, appID)
  if checked and running then
    openAttempted = true
    pcall(ac.setAppOpen, appID)
  end
end
