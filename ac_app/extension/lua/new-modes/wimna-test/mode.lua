-- The custom mode opens the director window once after CSP loads its associated app.
-- This does not start a run or change vehicle physics, controls, or RSS settings.
local appID = 'WimnaTestDirector'
local openAttempted = false
local modeStamped = false

function script.update(dt)
  if not modeStamped then
    modeStamped = pcall(function() ac.store('wimna.session_mode.v1', 'wimna-test') end)
  end
  if openAttempted or type(ac.isLuaAppRunning) ~= 'function' or type(ac.setAppOpen) ~= 'function' then
    return
  end

  local checked, running = pcall(ac.isLuaAppRunning, appID)
  if checked and running then
    openAttempted = true
    pcall(ac.setAppOpen, appID)
  end
end
