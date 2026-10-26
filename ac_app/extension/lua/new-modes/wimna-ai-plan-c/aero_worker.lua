-- Physics-rate prototype force proxy. Units: speed m/s, density kg/m^3,
-- equivalent areas m^2, force N. Ambient air is read from the simulator;
-- pressure is a declared sea-level assumption, not measured track pressure.
local p = __input
local origin = vec3()
local drag = vec3()
local down = vec3()
local sim = ac.getSim()
local forceCallPairs = 0
local reportElapsedS = 0

function script.update(dt)
  if type(dt) ~= 'number' or dt <= 0 or dt > p.maxDtS then return end
  local velocity = physics.getPointVelocity(0, origin, true)
  if not velocity then return end
  local speedSq = velocity.x * velocity.x + velocity.y * velocity.y + velocity.z * velocity.z
  if speedSq < 1 then return end
  local speed = math.sqrt(speedSq)
  local cappedSpeed = math.min(speed, p.maxSpeedMS)
  local ambient = sim and tonumber(sim.ambientTemperature) or nil
  if not ambient or ambient < -30 or ambient > 70 then ambient = p.ambientFallbackC end
  local density = p.pressurePa / (287.05 * (ambient + 273.15))
  local q = 0.5 * density * cappedSpeed * cappedSpeed
  -- SDK requests dt scaling for repeated addForce calls. Normalize to its
  -- nominal 333 Hz physics step and cap each issued force after scaling.
  local stepScale = dt / p.nominalStepS
  local dragN = math.min(p.maxDragN, q * p.deltaCdA * stepScale)
  local downN = math.min(p.maxDownforceN, q * p.deltaDownforceArea * stepScale)
  drag.x, drag.y, drag.z = -velocity.x * dragN / speed,
    -velocity.y * dragN / speed, -velocity.z * dragN / speed
  down.x, down.y, down.z = 0, -downN, 0
  physics.addForce(0, origin, true, drag, false)
  physics.addForce(0, origin, true, down, true)
  -- Both calls returned without a Lua error. The API has no applied-force
  -- readback, so this counter verifies issued calls only. Keep shared-store
  -- writes at 1 Hz rather than at the 333 Hz physics-worker rate.
  forceCallPairs = forceCallPairs + 1
  reportElapsedS = reportElapsedS + dt
  if reportElapsedS >= 1 then
    reportElapsedS = reportElapsedS - 1
    pcall(ac.store, 'wimna.prototype.force_call_pairs.v1', forceCallPairs)
    pcall(ac.store, 'wimna.prototype.last_drag_n.v1', dragN)
    pcall(ac.store, 'wimna.prototype.last_downforce_n.v1', downN)
    pcall(ac.store, 'wimna.prototype.last_speed_m_s.v1', speed)
    pcall(ac.store, 'wimna.prototype.worker_telemetry_status.v1',
      'force_calls_issued_effect_unverified')
  end
end
