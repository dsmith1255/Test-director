-- Open the director when this AI-capable session mode starts.
-- The app can now start a labeled collection run; AI control is still
-- requested through the app and checked against native readback.
local appID = 'WimnaTestDirector'
local openAttempted = false
local modeStamped = false

function script.update(dt)
  if not modeStamped then
    modeStamped = pcall(function() ac.store('wimna.session_mode.v1', 'wimna-ai-thermal') end)
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
