-- WIMNA Test Director: CSP Lua app. A collection run can contain human and AI
-- intervals; the controller is observed per sample. The separate WIMNA AI
-- Thermal session mode requests extended physics for optional AI takeover.
-- Telemetry field probes are guarded because Tyler's installed CSP SDK is the authority.

local SCHEMA = 1
local SAMPLE_PERIOD_S = 0.05
local CHUNK_PERIOD_S = 1.0
local MAX_BUFFER_LINES = 120
local AI_SWITCH_TIMEOUT_S = 3.0
local AI_RELEASE_MAX_SPEED_KMH = 2.0
local NULL = {}
local EMPTY_ARRAY = setmetatable({}, { __wimna_array = true })
local sim = ac.getSim()
local car = ac.getCar(0)

local function readField(object, key)
  if object == nil then return nil end
  local ok, value = pcall(function() return object[key] end)
  if not ok then return nil end
  if type(value) == 'number' and (value ~= value or value == math.huge or value == -math.huge) then
    return nil
  end
  if type(value) == 'number' or type(value) == 'boolean' or type(value) == 'string' then
    return value
  end
  return nil
end

local function safeCall(fn, fallback)
  local ok, value = pcall(fn)
  if ok and value ~= nil then return value end
  return fallback
end

local function monotonicNow()
  return safeCall(function() return os.preciseClock() end,
    safeCall(function() return os.clock() end, 0))
end

local function aiReadback()
  local value = readField(car, 'isAIControlled')
  if type(value) == 'boolean' then return value end
  return nil
end

-- Never give up native steering from this app while the car is moving. A pit
-- lane flag alone is insufficient: the car can still be travelling at speed.
local function aiReleaseSafe()
  local speed = readField(car, 'speedKmh')
  return type(speed) == 'number' and speed >= 0 and speed <= AI_RELEASE_MAX_SPEED_KMH,
    speed, readField(car, 'isInPitlane'), readField(car, 'isInPit')
end

-- CSP can expose callable native members as cdata rather than Lua functions.
-- Probe each gate independently; the setter itself is only called on a request.
local function probePhysicsAutopilot()
  local probe = {
    physics_type = type(physics),
    allowed_member_type = 'nil',
    allowed_member_access_ok = false,
    allowed_call_ok = false,
    allowed_value = NULL,
    allowed_value_type = 'unavailable',
    set_car_autopilot_member_type = 'nil',
    set_car_autopilot_member_access_ok = false,
    extended_physics = NULL,
    extended_physics_type = 'unavailable',
    extended_physics_read_ok = false
  }
  local allowedAccessOk, allowedMember = pcall(function() return physics.allowed end)
  probe.allowed_member_access_ok = allowedAccessOk
  if allowedAccessOk then
    probe.allowed_member_type = type(allowedMember)
    if allowedMember ~= nil then
      local allowedCallOk, allowedValue = pcall(function() return allowedMember() end)
      probe.allowed_call_ok = allowedCallOk
      if allowedCallOk then
        probe.allowed_value_type = type(allowedValue)
        if type(allowedValue) == 'boolean' then probe.allowed_value = allowedValue end
      else
        probe.allowed_call_error = tostring(allowedValue)
      end
    end
  else
    probe.allowed_member_access_error = tostring(allowedMember)
  end
  local setterAccessOk, setterMember = pcall(function() return physics.setCarAutopilot end)
  probe.set_car_autopilot_member_access_ok = setterAccessOk
  if setterAccessOk then
    probe.set_car_autopilot_member_type = type(setterMember)
  else
    probe.set_car_autopilot_member_access_error = tostring(setterMember)
  end
  local extendedReadOk, extendedValue = pcall(function() return car.extendedPhysics end)
  probe.extended_physics_read_ok = extendedReadOk
  if extendedReadOk then
    probe.extended_physics_type = type(extendedValue)
    if type(extendedValue) == 'boolean' then probe.extended_physics = extendedValue end
  else
    probe.extended_physics_read_error = tostring(extendedValue)
  end
  return probe
end

local function physicsAutopilotStatus(probe)
  if not probe.allowed_member_access_ok then return 'physics_allowed_access_failed' end
  if probe.allowed_member_type == 'nil' then return 'physics_allowed_unavailable' end
  if not probe.allowed_call_ok then return 'physics_allowed_call_failed' end
  if probe.allowed_value == false then return 'physics_permission_denied' end
  if probe.allowed_value ~= true then return 'physics_allowed_nonboolean' end
  if not probe.set_car_autopilot_member_access_ok then return 'autopilot_api_access_failed' end
  if probe.set_car_autopilot_member_type == 'nil' then return 'autopilot_api_unavailable' end
  return 'permission_granted_effect_unverified'
end

local function physicsAutopilotAllowed(probe)
  return physicsAutopilotStatus(probe or probePhysicsAutopilot()) == 'permission_granted_effect_unverified'
end

local function json(value)
  if value == NULL or value == nil then return 'null' end
  local kind = type(value)
  if kind == 'number' then
    if value ~= value or value == math.huge or value == -math.huge then return 'null' end
    return tostring(value)
  end
  if kind == 'boolean' then return value and 'true' or 'false' end
  if kind == 'string' then return JSON.stringify(value) end
  if kind == 'table' then
    local meta = getmetatable(value)
    local array = #value > 0 or (meta and meta.__wimna_array == true)
    local parts = {}
    if array then
      for i = 1, #value do parts[#parts + 1] = json(value[i]) end
      return '[' .. table.concat(parts, ',') .. ']'
    end
    for key, item in pairs(value) do
      parts[#parts + 1] = JSON.stringify(tostring(key)) .. ':' .. json(item)
    end
    return '{' .. table.concat(parts, ',') .. '}'
  end
  return 'null'
end

local function channel(value, unit, source, quality)
  return {
    value = value == nil and NULL or value,
    unit = unit,
    source = source or 'CSP_STATE',
    quality = quality or (value == nil and 'unavailable' or 'available')
  }
end

local function nullableBoolean(value)
  if type(value) == 'boolean' then return value end
  return NULL
end

local function utcNow()
  local systemTime = readField(sim, 'systemTime')
  if type(systemTime) == 'number' and systemTime > 0 then
    local formatted = safeCall(function()
      return os.dateGlobal('%Y-%m-%dT%H:%M:%SZ', systemTime)
    end, nil)
    if type(formatted) == 'string' and formatted ~= '' then return formatted end
  end
  return safeCall(function() return os.date('!%Y-%m-%dT%H:%M:%SZ') end, '')
end

local function pathJoin(a, b)
  return a .. '/' .. b
end

local documents = safeCall(function() return ac.getFolder(ac.FolderID.Documents) end, nil)
local bridge = documents and pathJoin(documents, 'WIMNA Test Director/bridge') or nil
local trackId = safeCall(function() return ac.getTrackID() end, nil)
local carId = safeCall(function() return ac.getCarID(0) end, nil)
local trackFolder = safeCall(function() return ac.getFolder(ac.FolderID.CurrentTrack) end, nil)
local layoutFolder = safeCall(function() return ac.getFolder(ac.FolderID.CurrentTrackLayout) end, nil)
local layoutId = safeCall(function() return ac.getTrackLayout() end, nil)
if layoutId == '' then layoutId = 'default' end
if not layoutId and trackFolder and layoutFolder then
  local a = trackFolder:gsub('\\', '/'):lower():gsub('/+$', '')
  local b = layoutFolder:gsub('\\', '/'):lower():gsub('/+$', '')
  if a == b then
    layoutId = 'default'
  elseif b:sub(1, #a + 1) == a .. '/' then
    layoutId = layoutFolder:gsub('\\', '/'):match('([^/]+)/?$')
  end
end

local cards = {
  C = { laps = '3-5 learning', instruction = 'Keep one SOC target. Avoid Boost/Overtake. Complete three accepted learning laps, then freeze or save the profile.', next = 'Confirm FRZ AUTO or the applied Manual copy.' },
  R = { laps = '5 timed', instruction = 'Drive the fastest clean pace you can repeat on reference fuel, tire and energy settings.', next = 'Box, save, and prepare contrasting fuel.' },
  F = { laps = '2 timed', instruction = 'Use the prescribed contrasting fuel load. Match tire preparation, battery start and deployment.', next = 'Box and prepare energy contrast.' },
  E = { laps = '4 timed', instruction = 'Higher deployment for laps 1-2; recovery map for laps 3-4. Change maps only at marked lap boundaries.', next = 'Record end battery state and remaining energy debt.' },
  M = { laps = '3 timed', instruction = 'Use baseline energy map and the track-specific management cues selected from reference telemetry.', next = 'Box and register validation prediction.' },
  V = { laps = '3 timed', instruction = 'Follow the frozen validation condition. Warm-start power restriction only if verified; otherwise held-out baseline/management.', next = 'Save, review residuals, and move to next track.' }
}

local state = {
  runId = nil,
  status = 'idle',
  mode = nil,
  blockId = 'C',
  seq = 0,
  chunkIndex = 0,
  buffer = {},
  writeOk = false,
  writeError = nil,
  droppedSamples = 0,
  sampleCount = 0,
  sampleAccumulator = 0,
  chunkAccumulator = 0,
  stateAccumulator = 0,
  directorAccumulator = 0,
  simTimeS = 0,
  clockSource = 'ac.getGameDeltaT',
  lastLapCount = nil,
  firstLapBoundaryPending = false,
  lastSessionIndex = nil,
  lastResetCounter = nil,
  nativeSessionEpoch = 0,
  currentLapFlags = {},
  director = nil,
  issuePanel = false,
  issueText = '',
  skipPanel = false,
  skipReason = '',
  profileText = '',
  individualPanel = false,
  lastStartChoice = nil,
  aiObserved = nil,
  aiAutoRequested = false,
  aiRequest = nil,
  aiControlOrigin = nil,
  aiNotice = nil,
  aiStopUnverified = false,
  autoLaunchAttempted = false,
  autoLaunchStatus = 'not attempted',
  collectionTireLabel = '',
  collectionRequestedCompound = '',
  collectionConditionLabel = '',
  collectionSetupLabel = '',
  collectionStintId = nil,
  collectionStintNumber = 0,
  collectionStintMeta = nil,
  collectionLastCompound = nil,
  collectionLastPitlane = nil,
  collectionLastPit = nil,
  collectionNotice = nil,
  startupUtc = utcNow()
}

local function maybeLaunchCompanion()
  if state.autoLaunchAttempted then return end
  state.autoLaunchAttempted = true
  local origin = safeCall(function() return ac.getFolder(ac.FolderID.ScriptOrigin) end, nil)
  if not origin then
    state.autoLaunchStatus = 'App folder unavailable; double-click the packaged companion.'
    return
  end
  local executable = pathJoin(origin, 'wimna-director.exe')
  local exists = safeCall(function() return io.fileExists(executable) end, false)
  if not exists then
    state.autoLaunchStatus = 'Companion not bundled; double-click it before driving.'
    return
  end
  if type(os.runConsoleProcess) ~= 'function' then
    state.autoLaunchStatus = 'CSP process API unavailable; double-click the companion.'
    return
  end
  local ok = pcall(function()
    os.runConsoleProcess({ filename = executable, workingDirectory = origin, terminateWithScript = false },
      function(err)
        if err then state.autoLaunchStatus = 'Companion exited or launch failed: ' .. tostring(err) end
      end)
  end)
  state.autoLaunchStatus = ok and 'Companion launch requested; waiting for director state.'
    or 'Companion launch was rejected; double-click it.'
end

local function runDirectory()
  return state.runId and pathJoin(pathJoin(bridge, 'runs'), state.runId) or nil
end

local function physicalSessionId()
  return state.runId and string.format('%s-s%d', state.runId, state.nativeSessionEpoch) or NULL
end

local function envelope(kind, data)
  state.seq = state.seq + 1
  return {
    schema = SCHEMA,
    run_id = state.runId,
    physical_session_id = physicalSessionId(),
    seq = state.seq,
    sim_time_s = state.simTimeS,
    wall_time_utc = utcNow(),
    kind = kind,
    data = data
  }
end

local function queue(record, isSample)
  if #state.buffer >= (isSample and MAX_BUFFER_LINES - 20 or MAX_BUFFER_LINES) then
    if isSample then state.droppedSamples = state.droppedSamples + 1 end
    return false
  end
  -- Keep at most one second of records in memory.  The completed chunk gets a
  -- channel dictionary at flush time, so the same unit/source/quality strings
  -- are not repeated 20 times for every one of the ~170 sample channels.
  state.buffer[#state.buffer + 1] = record
  return true
end

local function compactSampleSchema(buffer)
  local first = nil
  for _, record in ipairs(buffer) do
    if record.kind == 'sample' then first = record; break end
  end
  if not first then return nil, nil end
  local names = {}
  for name in pairs(first.data) do names[#names + 1] = name end
  table.sort(names)
  local channels = {}
  for _, name in ipairs(names) do
    local item = first.data[name]
    channels[#channels + 1] = { name, item.unit, item.source }
  end
  return {
    ['$'] = 'WIMNA_COMPACT_JSONL_V1',
    schema = first.schema,
    run_id = first.run_id,
    physical_session_id = first.physical_session_id,
    channels = channels
  }, names
end

local function compactSample(record, header, names)
  if record.kind ~= 'sample' then return json(record) end
  local values = setmetatable({}, { __wimna_array = true })
  local qualities = setmetatable({}, { __wimna_array = true })
  local count = 0
  for _ in pairs(record.data) do count = count + 1 end
  if count ~= #names then return json(record) end
  for index, name in ipairs(names) do
    local item = record.data[name]
    local descriptor = header.channels[index]
    if type(item) ~= 'table' or item.unit ~= descriptor[2] or item.source ~= descriptor[3] then
      -- Future adapters may change units or channel sets mid-chunk.  Preserve
      -- those samples verbatim rather than silently applying an old descriptor.
      return json(record)
    end
    local value = item.value == nil and NULL or item.value
    values[index] = value
    local defaultQuality = value == NULL and 'unavailable' or 'available'
    if item.quality ~= defaultQuality then
      qualities[#qualities + 1] = { index, item.quality }
    end
  end
  local payload = {
    ['$'] = 's', s = record.seq, t = record.sim_time_s,
    w = record.wall_time_utc, v = values, q = qualities
  }
  if record.physical_session_id ~= header.physical_session_id then
    payload.p = record.physical_session_id
  end
  return json(payload)
end

local function flush()
  if not state.runId or #state.buffer == 0 or not state.writeOk then return end
  local filename = pathJoin(runDirectory(), string.format('%06d.jsonl', state.chunkIndex + 1))
  local header, names = compactSampleSchema(state.buffer)
  local lines = {}
  if header then lines[#lines + 1] = json(header) end
  for _, record in ipairs(state.buffer) do
    lines[#lines + 1] = header and compactSample(record, header, names) or json(record)
  end
  -- A reader can see a file while io.save is still writing it.  The footer
  -- makes such an incomplete chunk ineligible until the write has finished.
  if header then lines[#lines + 1] = json({ ['$'] = 'e', n = #state.buffer }) end
  local payload = table.concat(lines, '\n') .. '\n'
  local ok, result = pcall(function() return io.save(filename, payload) end)
  if ok and result ~= false then
    state.chunkIndex = state.chunkIndex + 1
    state.buffer = {}
    state.writeError = nil
  else
    state.writeOk = false
    state.writeError = 'Chunk write failed; telemetry buffer bounded at ' .. MAX_BUFFER_LINES .. ' records.'
  end
end

local function emitEvent(eventType, extra)
  if not state.runId then return end
  local data = extra or {}
  data.type = eventType
  queue(envelope('event', data), false)
  flush()
end

local writeCurrentState

local function observedCompound()
  local value = safeCall(function() return car:tyresName() end, nil)
  return type(value) == 'string' and value ~= '' and value or nil
end

local function sessionModeReadback()
  local value = safeCall(function() return ac.load('wimna.session_mode.v1') end, nil)
  if value == 'wimna-test' or value == 'wimna-ai-thermal' then return value end
  return nil
end

local function nativeSessionName()
  local index = readField(sim, 'currentSessionIndex')
  if type(index) ~= 'number' then return nil end
  return safeCall(function() return ac.getSessionName(index) end, nil)
end

local function optionalText(value)
  if type(value) ~= 'string' then return NULL end
  local trimmed = value:match('^%s*(.-)%s*$')
  return trimmed ~= '' and trimmed or NULL
end

local function normalizedCompoundName(value)
  if type(value) ~= 'string' then return nil end
  local normalized = value:lower():gsub('[%s%p_]+', '')
  return normalized
end

local function collectionEnabled()
  return state.mode == 'collection' or state.mode == 'ai_thermal'
end

local function collectionBoundaryData(reason)
  local meta = state.collectionStintMeta or {}
  return {
    collection_id = state.runId,
    collection_stint_id = state.collectionStintId or NULL,
    stint_number = state.collectionStintNumber,
    stint_physical_session_id = meta.physical_session_id or NULL,
    stint_session_mode = meta.session_mode or NULL,
    stint_extended_physics = nullableBoolean(meta.extended_physics),
    planned_tire_label = meta.planned_tire_label or NULL,
    requested_tyre_compound = meta.requested_tyre_compound or NULL,
    condition_label = meta.condition_label or NULL,
    setup_label_requested = meta.setup_label_requested or NULL,
    setup_label_source = meta.setup_label_requested and meta.setup_label_requested ~= NULL and
      'DRIVER_ENTERED_UNVERIFIED' or NULL,
    observed_tyre_compound = observedCompound() or NULL,
    observed_air_c = readField(sim, 'ambientTemperature') or NULL,
    observed_track_c = readField(sim, 'roadTemperature') or NULL,
    session_mode_id = sessionModeReadback() or NULL,
    session_mode = sessionModeReadback() or NULL,
    extended_physics_observed = nullableBoolean(readField(car, 'extendedPhysics')),
    extended_physics = nullableBoolean(readField(car, 'extendedPhysics')),
    setup_hash = NULL,
    profile_hash = NULL,
    native_session_name = nativeSessionName() or NULL,
    native_session_type = readField(sim, 'raceSessionType') or NULL,
    observed_ai_control = nullableBoolean(aiReadback()),
    observed_user_control = nullableBoolean(readField(car, 'isUserControlled')),
    reason = reason
  }
end

local function endCollectionStint(reason)
  if not state.collectionStintId then return end
  local data = collectionBoundaryData(reason)
  data.started_sim_time_s = state.collectionStintMeta.started_sim_time_s
  data.elapsed_sim_time_s = state.simTimeS - data.started_sim_time_s
  emitEvent('collection_stint_end', data)
  state.collectionStintId = nil
  state.collectionStintMeta = nil
  state.collectionNotice = 'Stint saved. Fit the next tire or condition, then start another stint; recording continues.'
  writeCurrentState()
end

local function startCollectionStint(reason)
  if not collectionEnabled() or not state.runId or
    (state.status ~= 'active' and state.status ~= 'paused') or state.collectionStintId then return end
  state.collectionStintNumber = state.collectionStintNumber + 1
  state.collectionStintId = string.format('%s-t%03d', state.runId, state.collectionStintNumber)
  -- A new stint can begin partway through a native lap; the next crossing is an out-lap.
  state.firstLapBoundaryPending = true
  state.collectionStintMeta = {
    physical_session_id = physicalSessionId(),
    session_mode = sessionModeReadback() or NULL,
    extended_physics = readField(car, 'extendedPhysics'),
    started_sim_time_s = state.simTimeS,
    planned_tire_label = optionalText(state.collectionTireLabel),
    requested_tyre_compound = optionalText(state.collectionRequestedCompound),
    condition_label = optionalText(state.collectionConditionLabel),
    setup_label_requested = optionalText(state.collectionSetupLabel),
    observed_tyre_compound_at_start = observedCompound() or NULL
  }
  state.collectionNotice = nil
  local data = collectionBoundaryData(reason)
  data.observed_tyre_compound_at_start = state.collectionStintMeta.observed_tyre_compound_at_start
  emitEvent('collection_stint_start', data)
  local requested = state.collectionStintMeta.requested_tyre_compound
  local observed = observedCompound()
  if requested ~= NULL and observed and normalizedCompoundName(requested) ~= normalizedCompoundName(observed) then
    emitEvent('collection_compound_label_mismatch', {
      collection_id = state.runId, collection_stint_id = state.collectionStintId,
      requested_tyre_compound = requested, observed_tyre_compound = observed,
      comparison = 'normalized_literal_labels_possible_alias'
    })
    state.collectionNotice = 'Requested tire label differs from the observed native label. Check the compound or its name before relying on this stint.'
  end
  state.collectionLastCompound = observedCompound()
  state.collectionLastPitlane = readField(car, 'isInPitlane')
  state.collectionLastPit = readField(car, 'isInPit')
  writeCurrentState()
end

writeCurrentState = function()
  if not bridge then return end
  local observedAI = aiReadback()
  local requestedAI = NULL
  if state.aiRequest then requestedAI = state.aiRequest.desired end
  local data = {
    schema = SCHEMA,
    run_id = state.runId or NULL,
    physical_session_id = physicalSessionId(),
    status = state.status,
    mode = state.mode or NULL,
    collection_id = collectionEnabled() and (state.runId or NULL) or NULL,
    collection_stint_id = state.collectionStintId or NULL,
    collection_stint_number = state.collectionStintNumber,
    planned_tire_label = state.collectionStintMeta and state.collectionStintMeta.planned_tire_label or NULL,
    requested_tyre_compound = state.collectionStintMeta and state.collectionStintMeta.requested_tyre_compound or NULL,
    condition_label = state.collectionStintMeta and state.collectionStintMeta.condition_label or NULL,
    setup_label_requested = state.collectionStintMeta and state.collectionStintMeta.setup_label_requested or NULL,
    observed_tyre_compound = observedCompound() or NULL,
    session_mode_id = sessionModeReadback() or NULL,
    session_mode = sessionModeReadback() or NULL,
    extended_physics_observed = nullableBoolean(readField(car, 'extendedPhysics')),
    extended_physics = nullableBoolean(readField(car, 'extendedPhysics')),
    car_id = carId or NULL,
    track_id = trackId or NULL,
    layout_id = layoutId or NULL,
    sim_time_s = state.simTimeS,
    samples_attempted = state.sampleCount,
    dropped_samples = state.droppedSamples,
    write_ok = state.writeOk,
    write_error = state.writeError or NULL,
    clock_source = state.clockSource,
    sample_target_hz = 20,
    sample_effective_hz = state.simTimeS > 0 and (state.sampleCount - state.droppedSamples) / state.simTimeS or NULL,
    sample_source = 'CSP graphics-frame car state',
    last_start_choice = state.lastStartChoice or NULL,
    is_ai_controlled = nullableBoolean(observedAI),
    is_user_controlled = nullableBoolean(readField(car, 'isUserControlled')),
    ai_autopilot_probe = collectionEnabled() and probePhysicsAutopilot() or NULL,
    ai_control_origin = state.aiControlOrigin or NULL,
    ai_auto_requested = state.aiAutoRequested,
    ai_control_request = requestedAI,
    ai_stop_unverified = state.aiStopUnverified,
    updated_utc = utcNow()
  }
  pcall(function() io.save(pathJoin(bridge, 'current_state.json'), json(data), true) end)
end

local function readDirector()
  if not bridge then return end
  local ok, content = pcall(function() return io.load(pathJoin(bridge, 'director_state.json'), '') end)
  if not ok or not content or content == '' then
    state.director = nil
    return
  end
  local parsedOk, parsed = pcall(function() return JSON.parse(content) end)
  if parsedOk and type(parsed) == 'table' and parsed.schema == SCHEMA and parsed.run_id == state.runId then
    state.director = parsed
    if type(parsed.block_id) == 'string' then state.blockId = parsed.block_id end
  else
    state.director = nil
  end
end

local function probeStorage()
  if not bridge then return false, 'Documents folder unavailable in this CSP build' end
  local ok, result = pcall(function()
    io.createDir(bridge)
    io.createDir(pathJoin(bridge, 'runs'))
    local probe = pathJoin(bridge, 'storage_probe.json')
    local marker = json({ schema = SCHEMA, written_utc = utcNow() })
    if io.save(probe, marker, true) == false then return false end
    return io.load(probe, '') == marker
  end)
  if not ok or result ~= true then return false, 'Cannot write/read Documents bridge; check CSP I/O and folder permissions' end
  return true, nil
end

local function newRunId()
  local stamp = utcNow():gsub('[^0-9]', '')
  local tick = math.floor(safeCall(function() return os.preciseClock() end, 0) * 1000000) % 100000000
  return string.format('%s-%08d', stamp, tick)
end

local function finishRun(reason, aiStopVerified)
  local observedAI = aiReadback()
  local pendingEnable = state.aiRequest ~= nil and state.aiRequest.desired == true
  endCollectionStint(reason)
  emitEvent('ui_end', { reason = reason, ai_stop_verified = aiStopVerified == nil and NULL or aiStopVerified,
    ai_control_observed_at_end = nullableBoolean(observedAI),
    ai_enable_pending_at_end = pendingEnable,
    ai_control_retained = observedAI == true,
    campaign_eligible = state.mode ~= 'ai_thermal' and state.mode ~= 'collection',
    collection_id = collectionEnabled() and state.runId or NULL })
  state.status = 'ended'
  state.aiStopUnverified = aiStopVerified ~= true and (observedAI ~= false or pendingEnable)
  -- An accepted AI command can take effect after End Run is pressed. Keep its
  -- pending readback/timeout so a new run cannot assume control is settled.
  flush()
  writeCurrentState()
end

local function requestAiControl(desired, reason)
  if (state.mode ~= 'ai_thermal' and state.mode ~= 'collection') or
    not state.runId or (state.status == 'ended' and reason ~= 'post_run_safe_stop') then return false end
  if state.aiRequest then
    state.aiNotice = 'An AI-control request is still pending. Wait for native readback or its timeout before trying again.'
    emitEvent('ai_control_request_pending', { requested = desired,
      pending_requested = state.aiRequest.desired, control_reason = reason })
    return false
  end
  local observed = aiReadback()
  if observed == nil then
    state.aiNotice = 'AI control readback unavailable. Use the configured Toggle AI Driver binding, then check the native AI indicator.'
    emitEvent('ai_control_unavailable', { requested = desired, reason = 'native_readback_unavailable',
      control_reason = reason })
    return false
  end
  if observed == desired then
    emitEvent('ai_control_observed', { observed = observed, requested = desired,
      control_reason = reason, method = 'already_in_requested_state', effect_verified = true })
    return true
  end
  if desired == false and observed == true then
    local safe, speed, inPitlane, inPit = aiReleaseSafe()
    if not safe then
      emitEvent('ai_release_blocked_for_safety', {
        control_reason = reason, speed_kmh = speed or NULL,
        in_pitlane = nullableBoolean(inPitlane), in_pit = nullableBoolean(inPit),
        max_release_speed_kmh = AI_RELEASE_MAX_SPEED_KMH,
        reason = speed == nil and 'speed_readback_unavailable' or 'car_moving',
        requested = false, observed = true, effect_verified = false
      })
      state.aiNotice = 'AI remains ON. Stop only when speed readback is at or below 2 km/h. Keep AI driving, or exit the session using the game menu.'
      writeCurrentState()
      return false
    end
  end
  local autopilotProbe = probePhysicsAutopilot()
  local autopilotStatus = physicsAutopilotStatus(autopilotProbe)
  if not physicsAutopilotAllowed(autopilotProbe) then
    local method = 'ac.trySimKeyPressCommand_ACTIVATE_AI'
    local hotkeyMemberType = type(ac.trySimKeyPressCommand)
    local supported = hotkeyMemberType ~= 'nil'
    local ok, recognized = false, false
    if supported then
      ok, recognized = pcall(function() return ac.trySimKeyPressCommand('ACTIVATE_AI', false) end)
    end
    local accepted = ok and recognized == true
    local recognizedReadback = NULL
    if ok and type(recognized) == 'boolean' then recognizedReadback = recognized end
    emitEvent('ai_control_request', { requested = desired, observed_before = observed,
      method = method, api_call_succeeded = ok, command_recognized = recognizedReadback,
      effect_verified = false, control_reason = reason,
      limitation = autopilotStatus, physics_probe = autopilotProbe,
      hotkey_member_type = hotkeyMemberType,
      error = ok and NULL or (supported and tostring(recognized) or 'API_unavailable') })
    if accepted then
      state.aiControlOrigin = method
      state.aiRequest = { desired = desired, requestedAt = monotonicNow(), reason = reason,
        method = method }
      state.aiNotice = 'Native AI toggle sent; waiting for observed AI control ' .. (desired and 'ON.' or 'OFF.')
      return true
    end
    state.aiControlOrigin = 'manual_cm_hotkey'
    state.aiNotice = 'Automatic AI control unavailable (' .. autopilotStatus .. '). Use the Toggle AI Driver binding; accept AI data only after native readback shows ON.'
    return false
  end
  local ok, result = pcall(function() return physics.setCarAutopilot(desired, false) end)
  local returnBoolean = NULL
  if ok and type(result) == 'boolean' then returnBoolean = result end
  state.aiControlOrigin = 'physics.setCarAutopilot'
  emitEvent('ai_control_request', { requested = desired, observed_before = observed,
    method = 'physics.setCarAutopilot', api_call_succeeded = ok,
    api_request_accepted = ok and result ~= false,
    api_return_type = ok and type(result) or NULL,
    api_return_boolean = returnBoolean,
    effect_verified = false, control_reason = reason, physics_probe = autopilotProbe,
    error = ok and NULL or tostring(result) })
  if not ok or result == false then
    state.aiNotice = 'CSP rejected the autopilot request. Use the configured Toggle AI Driver binding and verify the native AI indicator.'
    return false
  end
  state.aiRequest = { desired = desired, requestedAt = monotonicNow(), reason = reason,
    method = 'physics.setCarAutopilot' }
  state.aiNotice = desired and 'Autopilot requested; waiting for native AI readback.'
    or 'Autopilot stop requested; waiting for native AI readback.'
  return true
end

local function endAiRun(reason)
  if (state.mode ~= 'ai_thermal' and state.mode ~= 'collection') or
    state.status == 'ended' then return end
  local observed = aiReadback()
  local safe, speed, inPitlane, inPit = aiReleaseSafe()
  local pendingEnable = state.aiRequest ~= nil and state.aiRequest.desired == true
  emitEvent('ai_end_requested', { reason = reason, observed = nullableBoolean(observed),
    speed_kmh = speed or NULL, release_safe = safe,
    in_pitlane = nullableBoolean(inPitlane), in_pit = nullableBoolean(inPit) })
  if observed == true then
    emitEvent('ai_control_retained_after_end', { speed_kmh = speed or NULL,
      max_release_speed_kmh = AI_RELEASE_MAX_SPEED_KMH,
      reason = 'explicit_safe_stop_required', release_safe = safe })
    state.aiNotice = 'Recording ended; AI remains ON. Keep AI driving and exit through the game menu, or stop safely and use Stop AI Driver at Rest.'
    finishRun(reason, false)
    return
  end
  if observed == nil then
    state.aiNotice = 'Recording ended with AI readback unavailable. No AI-OFF command was sent. Check native control before taking the wheel or exit the session.'
    finishRun(reason, nil)
    return
  end
  if observed == false and pendingEnable then
    state.aiNotice = 'Recording ended while AI takeover was pending. No AI-OFF command was sent. Check native control before taking the wheel or exit the session.'
    finishRun(reason, nil)
    return
  end
  if observed == false then
    finishRun(reason, true)
    return
  end
end

local function pollAiControl()
  if (state.mode ~= 'ai_thermal' and state.mode ~= 'collection') or
    not state.runId or (state.status == 'ended' and
      not state.aiStopUnverified and state.aiRequest == nil) then return end
  local observed = aiReadback()
  if observed ~= state.aiObserved then
    local method = state.aiRequest and state.aiRequest.method or 'external_or_manual'
    if observed == true and state.aiRequest == nil then state.aiControlOrigin = method end
    emitEvent('ai_control_observed', { observed = nullableBoolean(observed),
      previous = nullableBoolean(state.aiObserved),
      requested = nullableBoolean(state.aiRequest and state.aiRequest.desired),
      method = method,
      effect_verified = state.aiRequest ~= nil and observed == state.aiRequest.desired })
    state.aiObserved = observed
    writeCurrentState()
    if state.status == 'ended' then flush() end
  end
  if state.aiRequest then
    if observed == state.aiRequest.desired then
      emitEvent('ai_control_effect_verified', { requested = state.aiRequest.desired,
        observed = observed, method = state.aiRequest.method,
        control_reason = state.aiRequest.reason })
      state.aiRequest = nil
      state.aiNotice = observed and 'AI control confirmed by native readback.'
        or 'Human control confirmed by native readback.'
      writeCurrentState()
      if state.status == 'ended' then flush() end
    elseif monotonicNow() - state.aiRequest.requestedAt >= AI_SWITCH_TIMEOUT_S then
      local endedPendingEnable = state.status == 'ended' and state.aiRequest.desired == true
      emitEvent('ai_control_timeout', { requested = state.aiRequest.desired,
        observed = nullableBoolean(observed), method = state.aiRequest.method,
        control_reason = state.aiRequest.reason })
      state.aiRequest = nil
      if endedPendingEnable and observed == false then
        -- No AI-OFF command was issued, so an unchanged OFF readback is not a
        -- verified AI stop. It only resolves the pending takeover timeout.
        state.aiStopUnverified = false
        state.aiNotice = 'AI takeover was not observed after the request timed out. Confirm native control before starting another run.'
        emitEvent('ai_enable_unverified_after_end', { observed = false,
          effect_verified = false, reason = 'request_timed_out' })
      else
        state.aiNotice = 'No native AI-control change was observed. Use the configured Toggle AI Driver binding and check readback.'
      end
      writeCurrentState()
      if state.status == 'ended' then flush() end
    end
  end
  if state.status == 'ended' then
    if observed == false and state.aiStopUnverified and
      not (state.aiRequest ~= nil and state.aiRequest.desired == true) then
      state.aiStopUnverified = false
      state.aiNotice = 'Native AI control is now OFF. Confirm you have the car before driving.'
      emitEvent('ai_stop_observed_after_run', { observed = false,
        effect_verified = true, method = 'native_readback' })
      flush()
      writeCurrentState()
    end
    return
  end
end

local function startRun(mode, block, autoAi)
  local requestAutoAi = autoAi == true or mode == 'ai_thermal'
  local observedAI = aiReadback()
  if state.aiRequest ~= nil then
    state.aiNotice = 'An AI-control request is still pending. Wait for native readback or its timeout before starting another run.'
    writeCurrentState()
    return
  end
  if state.runId and collectionEnabled() and state.status ~= 'ended' then
    endAiRun('new_start_choice')
    return
  end
  if observedAI == true or (state.aiStopUnverified and observedAI ~= false) then
    if mode ~= 'ai_thermal' and mode ~= 'collection' then
      state.aiNotice = 'AI control has not been observed off. Use Toggle AI Driver and wait for native readback before starting another run.'
      writeCurrentState()
      return
    end
  end
  if observedAI == false then state.aiStopUnverified = false end
  if state.runId and state.status ~= 'ended' then finishRun('new_start_choice', nil) end
  state.runId = newRunId()
  state.mode = mode
  state.lastStartChoice = mode
  state.blockId = block or 'C'
  state.status = 'active'
  state.seq = 0
  state.chunkIndex = 0
  state.buffer = {}
  state.droppedSamples = 0
  state.sampleCount = 0
  state.sampleAccumulator = 0
  state.chunkAccumulator = 0
  state.simTimeS = 0
  state.lastLapCount = readField(car, 'lapCount')
  -- The native lap already in progress might have started before this run.
  state.firstLapBoundaryPending = true
  state.lastSessionIndex = readField(sim, 'currentSessionIndex')
  state.lastResetCounter = readField(car, 'resetCounter')
  state.nativeSessionEpoch = 0
  state.currentLapFlags = {}
  state.director = nil
  state.aiObserved = observedAI
  state.aiAutoRequested = requestAutoAi
  state.aiRequest = nil
  state.aiControlOrigin = nil
  if mode == 'ai_thermal' or mode == 'collection' then
    if observedAI == true then
      state.aiControlOrigin = 'preexisting_unknown'
    elseif observedAI == nil then
      state.aiControlOrigin = 'readback_unavailable'
    else
      state.aiControlOrigin = requestAutoAi and (physicsAutopilotAllowed() and 'physics.setCarAutopilot'
        or (type(ac.trySimKeyPressCommand) ~= 'nil' and 'native_ai_hotkey_planned' or 'manual_cm_hotkey')
        ) or 'no_ai_request_at_start'
    end
  end
  state.aiNotice = nil
  state.collectionStintId = nil
  state.collectionStintNumber = 0
  state.collectionStintMeta = nil
  state.collectionLastCompound = nil
  state.collectionLastPitlane = nil
  state.collectionLastPit = nil
  state.collectionNotice = nil
  state.skipPanel = false
  state.skipReason = ''
  local storageOk, storageError = probeStorage()
  state.writeOk, state.writeError = storageOk, storageError
  if storageOk then
    local directoryOk = pcall(function() io.createDir(runDirectory()) end)
    if not directoryOk then
      state.writeOk = false
      state.writeError = 'Could not create run directory'
    end
    local manifest = {
      schema = SCHEMA,
      run_id = state.runId,
      physical_session_id = physicalSessionId(),
      started_utc = utcNow(),
      car_id = carId or NULL,
      track_id = trackId or NULL,
      layout_id = layoutId or NULL,
      selection = mode,
      session_mode_id = sessionModeReadback() or NULL,
      session_mode = sessionModeReadback() or NULL,
      session_mode_source = sessionModeReadback() and 'CSP_SESSION_SHARED_STORE' or 'unavailable',
      native_session_name = nativeSessionName() or NULL,
      native_session_type = readField(sim, 'raceSessionType') or NULL,
      native_session_index = readField(sim, 'currentSessionIndex') or NULL,
      extended_physics_observed = nullableBoolean(readField(car, 'extendedPhysics')),
      extended_physics = nullableBoolean(readField(car, 'extendedPhysics')),
      car_physics_available_observed = nullableBoolean(readField(car, 'physicsAvailable')),
      collection_id = (mode == 'collection' or mode == 'ai_thermal') and state.runId or NULL,
      collection_enabled = mode == 'collection' or mode == 'ai_thermal',
      planned_tire_label = (mode == 'collection' or mode == 'ai_thermal') and optionalText(state.collectionTireLabel) or NULL,
      requested_tyre_compound = (mode == 'collection' or mode == 'ai_thermal') and optionalText(state.collectionRequestedCompound) or NULL,
      condition_label = (mode == 'collection' or mode == 'ai_thermal') and optionalText(state.collectionConditionLabel) or NULL,
      setup_label_requested = (mode == 'collection' or mode == 'ai_thermal') and optionalText(state.collectionSetupLabel) or NULL,
      setup_label_source = (mode == 'collection' or mode == 'ai_thermal') and
        optionalText(state.collectionSetupLabel) ~= NULL and 'DRIVER_ENTERED_UNVERIFIED' or NULL,
      individual_block = block or NULL,
      app_version = '0.4.1',
      csp_version_code = safeCall(function() return ac.getPatchVersionCode() end, NULL),
      car_version = NULL,
      track_version = NULL,
      profile_hash = NULL,
      setup_hash = NULL,
      source = 'CSP app runtime probe',
      campaign_counted = mode ~= 'ai_thermal' and mode ~= 'collection',
      collection_data_status = (mode == 'collection' or mode == 'ai_thermal') and 'measured_raw_with_observed_controller' or NULL,
      ai_auto_requested = requestAutoAi,
      ai_control_origin = state.aiControlOrigin or NULL,
      ai_control_origin_status = (mode == 'ai_thermal' or mode == 'collection') and
        (observedAI == true and 'preexisting_unattributed' or
          observedAI == nil and 'readback_unavailable' or requestAutoAi and 'planned_route_unverified' or 'not_requested') or NULL,
      ai_control_observed_at_start = nullableBoolean(observedAI),
      ai_native_lap_policy = mode == 'ai_thermal' and 'campaign_separate_collection_data_usable' or NULL,
      note = 'A fresh run ID never proves continuity of a physical stint.'
    }
    local ok, result = pcall(function() return io.save(pathJoin(runDirectory(), 'manifest.json'), json(manifest)) end)
    if not ok or result == false then
      state.writeOk = false
      state.writeError = 'Could not write run manifest'
    end
  end
  if state.writeOk then
    local autopilotProbe = probePhysicsAutopilot()
    local capabilities = {
      recording = { status = 'runtime_probe_passed', path = 'Documents/WIMNA Test Director/bridge' },
      telemetry = { status = 'runtime_probed_per_field', source = 'CSP graphics-frame state' },
      custom_cm_session_type = { status = 'unverified' },
      rss_map_write = { status = 'unverified' },
      rss_map_readback = { status = 'unverified' },
      live_power_derating = { status = 'unsupported_in_this_app' },
      thermal_live_actuation = { status = 'analysis_only' },
      setting_changes = { status = 'driver_confirmed_only' },
      ai_control_readback = { status = aiReadback() == nil and 'unavailable' or 'runtime_probed',
        source = 'car.isAIControlled' },
      ai_autopilot_request = { status = physicsAutopilotStatus(autopilotProbe),
        source = 'physics.setCarAutopilot', probe = autopilotProbe },
      ai_hotkey_command = { status = type(ac.trySimKeyPressCommand) ~= 'nil' and 'api_present_effect_unverified' or 'unavailable',
        member_type = type(ac.trySimKeyPressCommand), source = 'ac.trySimKeyPressCommand(ACTIVATE_AI)' }
    }
    queue(envelope('capability', capabilities), false)
    local eventType = mode == 'full' and 'ui_start_full' or mode == 'resume' and 'ui_resume'
      or mode == 'ai_thermal' and 'ui_start_ai_thermal' or mode == 'collection' and 'ui_start_collection' or 'ui_individual'
    emitEvent(eventType, { block_id = block or NULL, car_id = carId or NULL,
      track_id = trackId or NULL, layout_id = layoutId or NULL,
      campaign_eligible = mode ~= 'ai_thermal' and mode ~= 'collection',
      collection_id = (mode == 'collection' or mode == 'ai_thermal') and state.runId or NULL,
      ai_auto_requested = requestAutoAi })
    if mode == 'collection' or mode == 'ai_thermal' then startCollectionStint('run_started') end
    if mode == 'ai_thermal' then
      emitEvent('ai_control_observed', { observed = nullableBoolean(observedAI),
        method = 'initial_readback', effect_verified = false })
      if observedAI == false then requestAiControl(true, 'ai_thermal_start') end
      if observedAI == nil then
        state.aiNotice = 'Native AI readback is unavailable; AI intervals cannot be verified. Check CSP and the Toggle AI Driver binding.'
      end
    elseif mode == 'collection' and autoAi then
      if observedAI == false then requestAiControl(true, 'collection_auto_ai_start') end
      if observedAI == nil then
        state.aiNotice = 'Native AI readback is unavailable; continue recording, but controller identity is unknown until it returns.'
      end
    end
  end
  writeCurrentState()
end

local function setLapFlag(flag)
  state.currentLapFlags[flag] = true
end

local function lapFlags()
  local flags = {}
  for flag, enabled in pairs(state.currentLapFlags) do
    if enabled then flags[#flags + 1] = flag end
  end
  table.sort(flags)
  state.currentLapFlags = {}
  if #flags == 0 then return EMPTY_ARRAY end
  return flags
end

local function detectSessionEvents()
  if not state.runId or state.status == 'ended' then return end
  local sessionIndex = readField(sim, 'currentSessionIndex')
  local lapCount = readField(car, 'lapCount')
  local resetCounter = readField(car, 'resetCounter')
  local sessionChanged = sessionIndex ~= nil and state.lastSessionIndex ~= nil and sessionIndex ~= state.lastSessionIndex
  local carReset = resetCounter ~= nil and state.lastResetCounter ~= nil and resetCounter ~= state.lastResetCounter
  local lapCounterChanged = lapCount ~= nil and state.lastLapCount ~= nil and
    (lapCount < state.lastLapCount or lapCount > state.lastLapCount + 1)
  if (sessionChanged or carReset or lapCounterChanged) and state.collectionStintId then
    emitEvent('collection_physical_state_changed', {
      collection_id = state.runId, collection_stint_id = state.collectionStintId,
      session_changed = sessionChanged, car_reset = carReset, lap_counter_changed = lapCounterChanged,
      action = 'stint_closed_requires_new_start'
    })
    endCollectionStint('physical_state_changed')
  end
  local physicalStateChanged = false
  if sessionIndex ~= nil and state.lastSessionIndex ~= nil and sessionIndex ~= state.lastSessionIndex then
    state.nativeSessionEpoch = state.nativeSessionEpoch + 1
    physicalStateChanged = true
    setLapFlag('session_changed')
    emitEvent('session_changed', { from = state.lastSessionIndex, to = sessionIndex,
      physical_session_id = physicalSessionId() })
  end
  if resetCounter ~= nil and state.lastResetCounter ~= nil and resetCounter ~= state.lastResetCounter then
    if not physicalStateChanged then state.nativeSessionEpoch = state.nativeSessionEpoch + 1 end
    physicalStateChanged = true
    setLapFlag('car_reset')
    emitEvent('car_reset', { from = state.lastResetCounter, to = resetCounter,
      physical_session_id = physicalSessionId() })
  end
  if lapCount ~= nil and state.lastLapCount ~= nil then
    if lapCount < state.lastLapCount then
      if not physicalStateChanged then state.nativeSessionEpoch = state.nativeSessionEpoch + 1 end
      physicalStateChanged = true
      setLapFlag('lap_counter_reset')
      emitEvent('lap_counter_reset', { from = state.lastLapCount, to = lapCount,
        physical_session_id = physicalSessionId() })
    elseif lapCount > state.lastLapCount + 1 then
      if not physicalStateChanged then state.nativeSessionEpoch = state.nativeSessionEpoch + 1 end
      physicalStateChanged = true
      setLapFlag('lap_counter_jump')
      emitEvent('lap_counter_jump', { from = state.lastLapCount, to = lapCount,
        physical_session_id = physicalSessionId() })
    elseif lapCount == state.lastLapCount + 1 and physicalStateChanged then
      state.firstLapBoundaryPending = false
      setLapFlag('physical_state_changed')
      emitEvent('lap_boundary_ambiguous', { from = state.lastLapCount, to = lapCount,
        reason = 'physical_state_changed_at_boundary',
        physical_session_id = physicalSessionId() })
    elseif lapCount == state.lastLapCount + 1 then
      local partialRunLap = state.firstLapBoundaryPending
      state.firstLapBoundaryPending = false
      local completed = lapCount
        -- car.lapTimeMs is the current lap timer and can reset at the line. Do not use it
        -- as a completed-lap time. The installed SDK defines previousLapTimeMs.
        local lastLapMs = readField(car, 'previousLapTimeMs')
        local lastLapS = type(lastLapMs) == 'number' and lastLapMs > 0 and lastLapMs / 1000 or NULL
        local lastValid = readField(car, 'isLastLapValid')
        if type(lastValid) ~= 'boolean' then lastValid = NULL end
        local flags = lapFlags()
        if partialRunLap then flags[#flags + 1] = 'partial_run_lap' end
        if state.status == 'paused' then flags[#flags + 1] = 'experiment_paused' end
        emitEvent('lap_complete', {
          lap_index = completed,
          lap_time_s = lastLapS,
          valid_native = lastValid,
          campaign_eligible = not collectionEnabled(),
          collection_id = collectionEnabled() and state.runId or NULL,
          collection_stint_id = state.collectionStintId or NULL,
          observed_tyre_compound = observedCompound() or NULL,
          ai_controlled_at_boundary = nullableBoolean(aiReadback()),
          last_lap_cuts_count = readField(car, 'lastLapCutsCount') or NULL,
          flags = flags,
          session_index = sessionIndex == nil and NULL or sessionIndex
        })
    end
  end
  if physicalStateChanged then state.firstLapBoundaryPending = true end
  state.lastLapCount = lapCount
  state.lastSessionIndex = sessionIndex
  state.lastResetCounter = resetCounter
  if readField(car, 'isInPitlane') == true then setLapFlag('pitlane') end
  if readField(car, 'isInPit') == true then setLapFlag('pit') end
  if readField(sim, 'isPaused') == true then setLapFlag('sim_paused') end
  if readField(car, 'justJumped') == true then setLapFlag('car_jump') end
  if state.status == 'paused' then setLapFlag('experiment_paused') end
  if collectionEnabled() then
    local inPitlane = readField(car, 'isInPitlane')
    local inPit = readField(car, 'isInPit')
    if (state.collectionLastPitlane ~= nil and inPitlane ~= state.collectionLastPitlane) or
      (state.collectionLastPit ~= nil and inPit ~= state.collectionLastPit) then
      emitEvent('collection_pit_transition', {
        collection_id = state.runId, collection_stint_id = state.collectionStintId or NULL,
        in_pitlane = nullableBoolean(inPitlane), in_pit = nullableBoolean(inPit),
        previous_in_pitlane = nullableBoolean(state.collectionLastPitlane),
        previous_in_pit = nullableBoolean(state.collectionLastPit),
        observed_tyre_compound = observedCompound() or NULL,
        observed_ai_control = nullableBoolean(aiReadback())
      })
    end
    state.collectionLastPitlane = inPitlane
    state.collectionLastPit = inPit
    local compound = observedCompound()
    if state.collectionLastCompound ~= nil and compound ~= nil and compound ~= state.collectionLastCompound then
      emitEvent('collection_compound_changed', {
        collection_id = state.runId, collection_stint_id = state.collectionStintId or NULL,
        previous_observed_tyre_compound = state.collectionLastCompound,
        observed_tyre_compound = compound,
        unexpected_within_stint = state.collectionStintId ~= nil,
        in_pitlane = nullableBoolean(inPitlane), in_pit = nullableBoolean(inPit)
      })
      if state.collectionStintId then
        setLapFlag('compound_changed_within_stint')
        endCollectionStint('observed_compound_changed')
        state.collectionNotice = 'Observed tire compound changed. The old stint was closed; label and start the new tire stint.'
      end
    end
    state.collectionLastCompound = compound
  end
end

local function wheelField(index, key)
  local ok, wheel = pcall(function() return car.wheels[index] end)
  return ok and readField(wheel, key) or nil
end

local function captureSample()
  if not state.runId or state.status == 'ended' then return end
  local c = {}
  local function add(name, object, field, unit)
    c[name] = channel(readField(object, field), unit)
  end
  add('speed_kmh', car, 'speedKmh', 'km/h')
  add('throttle', car, 'gas', 'fraction')
  add('brake', car, 'brake', 'fraction')
  add('steering_native', car, 'steer', 'deg')
  add('gear', car, 'gear', 'gear_index')
  add('rpm', car, 'rpm', 'rev/min')
  add('fuel', car, 'fuel', 'L')
  add('battery_soc_native', car, 'kersCharge', 'native_unit_unverified')
  add('kers_current_kj_native', car, 'kersCurrentKJ', 'kJ_native')
  add('kers_max_kj_native', car, 'kersMaxKJ', 'kJ_native')
  add('kers_input_native', car, 'kersInput', 'native_unit_unverified')
  add('kers_load_native', car, 'kersLoad', 'native_unit_unverified')
  add('fuel_map_observed', car, 'fuelMap', 'native_index')
  add('compound_index_native', car, 'compoundIndex', 'native_index')
  add('car_mass_kg_native', car, 'mass', 'kg')
  add('ballast_kg_native', car, 'ballast', 'kg')
  add('brake_bias_native', car, 'brakeBias', 'native_fraction_unverified')
  add('mguk_delivery_native', car, 'mgukDelivery', 'native_index')
  add('mguk_recovery_native', car, 'mgukRecovery', 'native_index')
  add('drs_active_native', car, 'drsActive', 'boolean')
  add('battery_voltage_native', car, 'batteryVoltage', 'V')
  add('drivetrain_power_native', car, 'drivetrainPower', 'native_unit_unverified')
  add('drivetrain_torque_native', car, 'drivetrainTorque', 'native_unit_unverified')
  add('engine_life_left_native', car, 'engineLifeLeft', 'native_life_not_temperature')
  add('gearbox_damage_native', car, 'gearboxDamage', 'native_fraction')
  add('wheels_outside_native', car, 'wheelsOutside', 'count')
  add('car_physics_available', car, 'physicsAvailable', 'boolean')
  add('distance_driven_session_km_native', car, 'distanceDrivenSessionKm', 'km')
  add('lap_index', car, 'lapCount', 'count')
  add('sector_index', car, 'currentSector', 'native_index')
  add('lap_time_current_ms', car, 'lapTimeMs', 'ms')
  add('spline_position', car, 'splinePosition', 'fraction_of_lap')
  add('in_pitlane', car, 'isInPitlane', 'boolean')
  add('in_pit', car, 'isInPit', 'boolean')
  add('lap_valid_current', car, 'isLapValid', 'boolean')
  add('session_index', sim, 'currentSessionIndex', 'index')
  add('sim_paused', sim, 'isPaused', 'boolean')
  add('sim_is_live', sim, 'isLive', 'boolean')
  add('sim_is_replay', sim, 'isReplayActive', 'boolean')
  local nativeWater = readField(car, 'waterTemperature')
  c.native_water_temperature_c_approx = channel(nativeWater, 'degC', 'CSP_STATE')
  add('native_oil_temperature', car, 'oilTemperature', 'native_unit_unverified')
  add('ambient_air_c', sim, 'ambientTemperature', 'degC')
  add('track_surface_c', sim, 'roadTemperature', 'degC')
  add('local_road_temperature_c', car, 'roadTemperature', 'degC')
  add('road_grip_native', sim, 'roadGrip', 'fraction')
  add('wind_speed_kmh', sim, 'windSpeedKmh', 'km/h')
  add('wind_direction_deg', sim, 'windDirectionDeg', 'deg')
  add('rain_intensity_native', sim, 'rainIntensity', 'native_unit_unverified')
  add('rain_wetness_native', sim, 'rainWetness', 'native_unit_unverified')
  add('rain_water_native', sim, 'rainWater', 'native_unit_unverified')
  add('weather_type_native', sim, 'weatherType', 'native_enum')
  add('time_multiplier_native', sim, 'timeMultiplier', 'native_multiplier')
  add('time_of_day_s', sim, 'timeTotalSeconds', 's_since_midnight')
  add('track_length_m', sim, 'trackLengthM', 'm')
  local spline = readField(car, 'splinePosition')
  local length = readField(sim, 'trackLengthM')
  c.track_distance_m = channel(type(spline) == 'number' and type(length) == 'number' and spline * length or nil,
    'm', 'PHYSICS_DERIVED', (spline and length) and 'derived_from_CSP_STATE' or 'unavailable')
  local tyreNames = { 'fl', 'fr', 'rl', 'rr' }
  for i = 0, 3 do
    local prefix = tyreNames[i + 1]
    c['tyre_wear_' .. prefix] = channel(wheelField(i, 'tyreWear'), 'native_fraction_not_life')
    c['tyre_virtual_km_' .. prefix] = channel(wheelField(i, 'tyreVirtualKM'), 'virtual_km_not_distance')
    c['tyre_grain_' .. prefix] = channel(wheelField(i, 'tyreGrain'), 'native_unit_unverified')
    c['tyre_blister_' .. prefix] = channel(wheelField(i, 'tyreBlister'), 'native_unit_unverified')
    c['tyre_flat_spot_' .. prefix] = channel(wheelField(i, 'tyreFlatSpot'), 'native_unit_unverified')
    c['tyre_optimum_c_' .. prefix] = channel(wheelField(i, 'tyreOptimumTemperature'), 'degC')
    c['tyre_static_pressure_' .. prefix] = channel(wheelField(i, 'tyreStaticPressure'), 'native_unit_unverified')
    c['wheel_camber_deg_' .. prefix] = channel(wheelField(i, 'camber'), 'deg')
    c['wheel_toe_deg_' .. prefix] = channel(wheelField(i, 'toeIn'), 'deg')
    c['tyre_loaded_radius_m_' .. prefix] = channel(wheelField(i, 'tyreLoadedRadius'), 'm')
    c['tyre_core_c_' .. prefix] = channel(wheelField(i, 'tyreCoreTemperature'), 'degC')
    c['tyre_inside_c_' .. prefix] = channel(wheelField(i, 'tyreInsideTemperature'), 'degC')
    c['tyre_middle_c_' .. prefix] = channel(wheelField(i, 'tyreMiddleTemperature'), 'degC')
    c['tyre_outside_c_' .. prefix] = channel(wheelField(i, 'tyreOutsideTemperature'), 'degC')
    c['tyre_pressure_' .. prefix] = channel(wheelField(i, 'tyrePressure'), 'native_unit_unverified')
    c['tyre_load_n_' .. prefix] = channel(wheelField(i, 'load'), 'N')
    c['wheel_slip_' .. prefix] = channel(wheelField(i, 'slipRatio'), 'native_proxy_unverified')
    c['wheel_slip_angle_deg_' .. prefix] = channel(wheelField(i, 'slipAngle'), 'deg')
    c['wheel_angular_speed_' .. prefix] = channel(wheelField(i, 'angularSpeed'), 'native_angular_speed_unverified')
    c['wheel_speed_difference_' .. prefix] = channel(wheelField(i, 'speedDifference'), 'native_speed_unverified')
    c['wheel_surface_grip_' .. prefix] = channel(wheelField(i, 'surfaceGrip'), 'native_fraction_unverified')
    c['wheel_surface_valid_track_' .. prefix] = channel(wheelField(i, 'surfaceValidTrack'), 'boolean')
    c['wheel_surface_dirt_' .. prefix] = channel(wheelField(i, 'surfaceDirt'), 'native_unit_unverified')
    c['brake_temperature_' .. prefix] = channel(wheelField(i, 'discTemperature'), 'native_scale_unverified')
    local nativeBrake = wheelField(i, 'brakeTemperature')
    c['brake_temperature_c_' .. prefix] = channel(nativeBrake, 'degC', 'CSP_STATE',
      nativeBrake == 0 and 'zero_may_mean_unavailable' or nil)
  end
  c.requested_power_w = channel(nil, 'W', 'RSS_UNVERIFIED')
  c.delivered_power_w = channel(nil, 'W', 'RSS_UNVERIFIED')
  c.recovery_power_w = channel(nil, 'W', 'RSS_UNVERIFIED')
  c.battery_energy_j = channel(nil, 'J', 'RSS_UNVERIFIED')
  c.engine_temperature_c = channel(nil, 'degC', 'RSS_UNVERIFIED')
  c.component_temperature_c = channel(nil, 'degC', 'RSS_UNVERIFIED')
  c.active_aero_state = channel(nil, 'native_state', 'RSS_UNVERIFIED')
  c.tyre_compound = channel(safeCall(function() return car:tyresName() end, nil), 'name', 'CSP_STATE')
  c.collection_id = channel(collectionEnabled() and state.runId or nil, 'identifier', 'WIMNA_APP')
  c.collection_stint_id = channel(state.collectionStintId, 'identifier', 'WIMNA_APP')
  c.is_ai_controlled = channel(aiReadback(), 'boolean', 'CSP_STATE')
  c.is_user_controlled = channel(readField(car, 'isUserControlled'), 'boolean', 'CSP_STATE')
  local aiLevel = readField(car, 'aiLevel')
  c.ai_level = channel(type(aiLevel) == 'number' and aiLevel >= 0 and aiLevel or nil,
    'fraction', 'CSP_STATE', type(aiLevel) == 'number' and aiLevel < 0 and 'not_applicable' or nil)
  local aiAggression = readField(car, 'aiAggression')
  c.ai_aggression = channel(type(aiAggression) == 'number' and aiAggression >= 0 and aiAggression or nil,
    'fraction', 'CSP_STATE', type(aiAggression) == 'number' and aiAggression < 0 and 'not_applicable' or nil)
  c.experiment_paused = channel(state.status == 'paused', 'boolean', 'WIMNA_APP')
  state.sampleCount = state.sampleCount + 1
  queue(envelope('sample', c), true)
end

function script.update(dt)
  maybeLaunchCompanion()
  if car == nil then car = ac.getCar(0) end
  local gameDt = safeCall(function() return ac.getGameDeltaT() end, nil)
  if type(gameDt) ~= 'number' or gameDt < 0 or gameDt > 2 then
    gameDt = readField(sim, 'isPaused') == true and 0 or (type(dt) == 'number' and math.max(0, math.min(dt, 0.25)) or 0)
    state.clockSource = 'graphics_dt_pause_gated_fallback'
  end
  state.simTimeS = state.simTimeS + gameDt
  state.directorAccumulator = state.directorAccumulator + (type(dt) == 'number' and dt or 0)
  state.stateAccumulator = state.stateAccumulator + (type(dt) == 'number' and dt or 0)
  if state.directorAccumulator >= 0.5 then
    state.directorAccumulator = 0
    readDirector()
  end
  if state.stateAccumulator >= 2 then
    state.stateAccumulator = 0
    writeCurrentState()
  end
  if not state.runId then return end
  pollAiControl()
  if state.status == 'ended' then return end
  detectSessionEvents()
  if gameDt > 0.2 then emitEvent('recording_gap', { duration_s = gameDt, reason = 'graphics_frame_interval' }) end
  state.sampleAccumulator = state.sampleAccumulator + gameDt
  state.chunkAccumulator = state.chunkAccumulator + gameDt
  if state.sampleAccumulator >= SAMPLE_PERIOD_S then
    -- One sample per graphics frame at most; never invent missing intermediate samples.
    state.sampleAccumulator = state.sampleAccumulator % SAMPLE_PERIOD_S
    captureSample()
  end
  if state.chunkAccumulator >= CHUNK_PERIOD_S then
    state.chunkAccumulator = 0
    flush()
  end
end

local function display(value, fallback)
  if value == nil or value == '' then return fallback or 'Unavailable' end
  return tostring(value)
end

local function collectionInputs()
  state.collectionTireLabel = ui.inputText('Tire test label (X/Y/Z)', state.collectionTireLabel)
  state.collectionRequestedCompound = ui.inputText('Requested tire compound', state.collectionRequestedCompound)
  state.collectionConditionLabel = ui.inputText('Condition label', state.collectionConditionLabel)
  state.collectionSetupLabel = ui.inputText('Setup / profile label', state.collectionSetupLabel)
  ui.textWrapped('Labels are driver-entered and unverified. The recorder saves observed tire and air/track conditions separately; setup/profile hashes remain unavailable.')
end

local function collectionStintControls()
  if state.collectionStintId then
    local meta = state.collectionStintMeta or {}
    ui.text('Stint: ' .. state.collectionStintId)
    ui.text('Tire: ' .. display(meta.planned_tire_label == NULL and nil or meta.planned_tire_label, 'unlabeled') ..
      ' | Requested: ' .. display(meta.requested_tyre_compound == NULL and nil or meta.requested_tyre_compound, 'unverified'))
    ui.text('Condition: ' .. display(meta.condition_label == NULL and nil or meta.condition_label, 'unlabeled'))
    ui.text('Setup/profile request: ' .. display(meta.setup_label_requested == NULL and nil or meta.setup_label_requested, 'unlabeled'))
    ui.text('Observed tire: ' .. display(observedCompound(), 'unavailable'))
    ui.text('Air / track: ' .. display(readField(sim, 'ambientTemperature'), '?') .. ' / ' ..
      display(readField(sim, 'roadTemperature'), '?') .. ' C')
    ui.textWrapped('Record the lap and pit entry, then end this stint. Telemetry continues while tires are changed.')
    if ui.button('Mark pit / control handoff') then
      emitEvent('collection_handoff_requested', {
        collection_id = state.runId, collection_stint_id = state.collectionStintId,
        observed_ai_control = nullableBoolean(aiReadback()),
        in_pitlane = nullableBoolean(readField(car, 'isInPitlane')),
        in_pit = nullableBoolean(readField(car, 'isInPit')),
        speed_kmh = readField(car, 'speedKmh') or NULL,
        action = 'driver_guidance_only'
      })
      state.collectionNotice = 'Handoff marked; no control changed. Bring the car to a stop, then use Stop AI Driver at Rest with a driver ready.'
    end
    if ui.button('End Stint / Box') then endCollectionStint('driver_boxed_or_requested') end
  else
    ui.textWrapped('No active labeled stint. Raw telemetry continues, including the tire change and out-lap.')
    collectionInputs()
    if ui.button('Start Next Stint') then startCollectionStint('driver_started_next_stint') end
  end
  if state.collectionNotice then ui.textWrapped(state.collectionNotice) end
end

function script.windowMain()
  ui.text('WIMNA Test Director')
  ui.text('Track: ' .. display(trackId, 'unavailable') .. (layoutId and (' / ' .. layoutId) or ''))
  ui.text('Car: ' .. display(carId, 'unavailable'))
  if state.runId == nil or state.status == 'ended' then
    ui.textWrapped(state.autoLaunchStatus)
    if state.aiNotice then ui.textWrapped(state.aiNotice) end
    local observedAI = aiReadback()
    if observedAI == true then
      local safe, speed = aiReleaseSafe()
      ui.textWrapped('Native AI control is ON at ' .. display(speed, 'unknown') ..
        ' km/h. Keep AI steering until the car is stopped. You can exit this Assetto Corsa session through its game menu while AI remains ON.')
      if collectionEnabled() and safe then
        if ui.button('Stop AI Driver at Rest') then requestAiControl(false, 'post_run_safe_stop') end
      elseif collectionEnabled() then
        ui.textWrapped('AI-OFF is locked until native speed readback is at or below 2 km/h. A pit flag alone does not release control.')
      end
    elseif observedAI == nil and state.runId then
      ui.textWrapped('AI control readback is unavailable. Treat control as unknown; do not release steering based on this app alone.')
    end
    if ui.button('Start Full Program') then startRun('full') end
    ui.sameLine()
    if ui.button('Resume') then startRun('resume') end
    if ui.button('Individual Test') then state.individualPanel = not state.individualPanel end
    if state.individualPanel then
      for _, id in ipairs({ 'C', 'R', 'F', 'E', 'M', 'V' }) do
        if ui.button('Start ' .. id .. ' (' .. cards[id].laps .. ')') then
          startRun('individual', id)
          state.individualPanel = false
        end
      end
    end
    ui.text('Open data collection')
    collectionInputs()
    if ui.button('Start Collection') then startRun('collection', nil, false) end
    ui.sameLine()
    if ui.button('Start AI Data Collection') then startRun('collection', nil, true) end
    ui.textWrapped('Collect tire, fuel, energy and thermal inputs in labeled stints. AI and human control are recorded per sample; both use simulator telemetry.')
    ui.textWrapped('For automatic AI takeover, select WIMNA AI Thermal in Content Manager. A pit handoff needs a driver ready to take control.')
    ui.textWrapped('Select the car and track in Content Manager first. A new start creates a new physical run.')
    return
  end

  local d = state.director
  local campaignBlock = d ~= nil and d.diagnostic_only ~= true and type(d.block_id) == 'string' and d.block_id ~= ''
  ui.text('Run: ' .. state.runId)
  ui.text('Experiment: ' .. state.status .. ' | Recorder: ' .. (state.writeOk and 'ON' or 'UNAVAILABLE'))
  if state.writeError then ui.textWrapped(state.writeError) end
  if state.firstLapBoundaryPending then
    ui.textWrapped('The first crossing after Start is recorded as a partial out-lap; begin counted laps after that line.')
  end
  if collectionEnabled() then
    local observedAI = aiReadback()
    local aiLabel = observedAI == nil and 'unavailable' or (observedAI and 'ON' or 'OFF')
    local autopilotProbe = probePhysicsAutopilot()
    ui.text('CSP autopilot API: ' .. physicsAutopilotStatus(autopilotProbe))
    ui.text('Extended physics readback: ' .. (autopilotProbe.extended_physics == NULL
      and 'unavailable' or tostring(autopilotProbe.extended_physics)))
    ui.text('AI control readback: ' .. aiLabel)
    ui.textWrapped('Collection data: simulator telemetry is recorded for either controller. Native validity, tire state and controller identity remain visible for later analysis.')
    collectionStintControls()
    if state.aiNotice then ui.textWrapped(state.aiNotice) end
    if state.status == 'paused' then
      if ui.button('Resume Collection') then
        state.status = 'active'
        emitEvent('ui_resume', { from_pause = true, collection_id = state.runId })
        writeCurrentState()
      end
    elseif ui.button('Pause Collection') then
      state.status = 'paused'
      emitEvent('ui_pause', { experiment_only = true, collection_id = state.runId })
      writeCurrentState()
    end
    ui.textWrapped('Pause marks the experiment; simulator driving and telemetry continue.')
    if observedAI == false then
      if ui.button('Request AI Driver') then requestAiControl(true, 'driver_button') end
    elseif observedAI == true then
      local safe = aiReleaseSafe()
      if safe then
        if ui.button('Stop AI Driver at Rest') then requestAiControl(false, 'driver_button_at_rest') end
      else
        ui.textWrapped('AI-OFF is locked while moving or speed is unknown. Keep AI driving until stopped, or end recording and exit this session through the game menu.')
      end
    end
    ui.textWrapped('End Collection Run closes recording. It never switches AI OFF; use the separate stop action only when stationary.')
    if ui.button('End Collection Run') then endAiRun('driver_requested') end
    if ui.button('Report Issue') then state.issuePanel = not state.issuePanel end
    if state.issuePanel then
      state.issueText = ui.inputText('Issue detail', state.issueText)
      for _, issue in ipairs({ 'Off-track', 'Spin', 'Contact', 'Damage', 'Unexpected setting', 'Other' }) do
        if ui.button(issue) then
          emitEvent('ui_issue', { issue = issue, detail = state.issueText,
            campaign_eligible = false, collection_id = state.runId,
            collection_stint_id = state.collectionStintId or NULL })
          setLapFlag(issue:lower():gsub(' ', '_'))
          state.issueText = ''
          state.issuePanel = false
        end
      end
    end
    ui.text('Samples: ' .. state.sampleCount .. ' | Dropped: ' .. state.droppedSamples)
    return
  end
  if campaignBlock then
    ui.text('Block: ' .. d.block_id .. ' | Laps: ' .. display(d.lap_progress, '—'))
    ui.textWrapped(display(d.instruction, 'Director instruction unavailable'))
    ui.textWrapped('Target: ' .. display(d.target, 'Numeric target pending configuration'))
    ui.textWrapped('Next: ' .. display(d.next_action, 'Review director status'))
    if ui.button('Confirm / Next') then
      emitEvent('ui_confirm', { block_id = d.block_id, director_state = d.state or NULL })
    end
    ui.sameLine()
    if ui.button('Confirm learning lap') then
      emitEvent('ui_confirm_learning', { block_id = d.block_id, note = 'Driver confirmed RSS learning acceptance' })
    end
    if ui.button('Confirm FRZ AUTO') then
      emitEvent('ui_confirm_profile', { block_id = d.block_id, mode = 'FRZ_AUTO', note = 'Driver confirmation; automatic RSS readback unavailable' })
    end
    state.profileText = ui.inputText('Manual profile name', state.profileText)
    if ui.button('Confirm Manual copy applied') and state.profileText:match('%S') then
      emitEvent('ui_confirm_profile', { block_id = d.block_id, mode = 'MANUAL_APPLIED', profile_name = state.profileText, note = 'Driver confirmation; profile hash/readback unavailable' })
    end
  else
    ui.text('Block: Diagnostic only | Laps: —')
    if d ~= nil then
      ui.textWrapped(display(d.instruction, 'Diagnostic recording; no campaign block is active for this run.'))
      ui.textWrapped('Target: ' .. display(d.target, 'No campaign lap target'))
      ui.textWrapped('Next: ' .. display(d.next_action, 'End this run and select a configured campaign track.'))
    else
      ui.textWrapped('Director offline or awaiting this run. Recording continues as diagnostic data; no campaign block is active until the companion confirms it.')
      ui.textWrapped(state.autoLaunchStatus)
    end
  end
  if d ~= nil and type(d.campaign_notice) == 'string' and d.campaign_notice ~= '' then
    ui.textWrapped(d.campaign_notice)
  end
  ui.textWrapped('Pause Test pauses the experiment; it does not necessarily pause the simulator. Telemetry stays on and is flagged.')
  if state.status == 'paused' then
    if ui.button('Resume Test') then
      state.status = 'active'
      emitEvent('ui_resume', { from_pause = true })
      writeCurrentState()
    end
  else
    if ui.button('Pause Test') then
      state.status = 'paused'
      emitEvent('ui_pause', { experiment_only = true })
      writeCurrentState()
    end
  end
  ui.sameLine()
  if ui.button('End Run') then
    finishRun('driver_requested', nil)
  end
  local canSkip = campaignBlock and (d.state == 'prepare_run' or d.state == 'calibration' or
    d.state == 'record_block' or d.state == 'map_transition' or
    (d.state == 'box_save' and state.blockId:match('C$')))
  if canSkip then
    if ui.button('Skip Incomplete Block...') then state.skipPanel = not state.skipPanel end
    if state.skipPanel then
      local validationSkip = state.blockId:match('V$') ~= nil
      if validationSkip then
        ui.textWrapped('Validation is protected. Skip it only when completion is unavoidable; enter the reason first.')
      else
        ui.textWrapped('Skipping closes this block with incomplete or partial evidence. Enter the reason first.')
      end
      state.skipReason = ui.inputText('Skip reason', state.skipReason)
      local confirmLabel = validationSkip and 'Confirm Unavoidable Validation Skip' or 'Confirm Skip Block'
      if ui.button(confirmLabel) and state.skipReason:match('%S') then
        emitEvent('ui_skip', { block_id = state.blockId, reason = state.skipReason,
          unavoidable = validationSkip })
        state.skipPanel = false
        state.skipReason = ''
      end
    end
  end
  if ui.button('Report Issue') then state.issuePanel = not state.issuePanel end
  if state.issuePanel then
    state.issueText = ui.inputText('Issue detail', state.issueText)
    for _, issue in ipairs({ 'Off-track', 'Spin', 'Contact', 'Damage', 'Unexpected setting', 'Other' }) do
      if ui.button(issue) then
        emitEvent('ui_issue', { issue = issue, detail = state.issueText })
        setLapFlag(issue:lower():gsub(' ', '_'))
        state.issueText = ''
        state.issuePanel = false
      end
    end
  end
  ui.text('Samples: ' .. state.sampleCount .. ' | Dropped: ' .. state.droppedSamples)
end
