-- Paired reference mode. The existing FH26 physics files remain untouched.
local appID = 'WimnaTestDirector'
local parameters = [[{"aero_force":null,"cooling_model_multiplier":1.0,"effect":"none","variant":"baseline","version":"wimna-prototype-v1"}]]
local parameterHash = '1b0fd209d1c46087bdbf3f8ff0ac6fe30472ae7938b29d662371772e0496bb12'
local stamped = false
local permissionProbed = false
local openAttempted = false

local function stamp()
  ac.store('wimna.session_mode.v1', 'wimna-ai-baseline')
  ac.store('wimna.prototype.variant.v1', 'baseline')
  ac.store('wimna.prototype.parameter_hash.v1', parameterHash)
  ac.store('wimna.prototype.parameters.v1', parameters)
  ac.store('wimna.prototype.effect.v1', 'none')
  ac.store('wimna.prototype.permission.v1', 0)
  ac.store('wimna.prototype.native_invalid_expected.v1', 0)
end

function script.update(dt)
  if not stamped then stamped = pcall(stamp) end
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
