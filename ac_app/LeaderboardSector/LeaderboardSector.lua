-- Leaderboard Sector companion app.
--
-- Polls the Lap Time Sender's local Flask server for one-shot commands and
-- executes them in-game via CSP's physics API. Used by the custom sector
-- challenge: when a driver misses a sector target, the Sender asks this app
-- to teleport the car back to the start/finish spawn for the next driver.
--
-- Protocol (plain text, no JSON so the Lua side stays trivial):
--   GET /api/ac/command  ->  'none'  or  'teleport|<id>|<spawn_set>'
--   GET /api/ac/ack?id=<id>&status=ok|error   (sent after executing)
-- Every poll doubles as a heartbeat so the Sender knows the app is alive.

-- The Sender prefers port 5001 but falls back if it is taken
-- (must match WEB_INTERFACE_FALLBACK_PORTS in lap_time_sender.py)
local SENDER_PORTS = { 5001, 5101, 5102, 5103, 5104, 5105 }
local POLL_PERIOD = 0.25

local portIndex = 1
local connected = false
local lastExecutedId = 0
local requestPending = false
local statusText = 'Looking for Lap Time Sender...'
local statusOk = false
local lastTeleportText = 'No teleports yet'

local function senderUrl()
  return 'http://127.0.0.1:' .. SENDER_PORTS[portIndex]
end

local function nextPort()
  connected = false
  portIndex = portIndex % #SENDER_PORTS + 1
end

local function sendAck(id, ok, detail)
  local status = ok and 'ok' or 'error'
  web.get(string.format('%s/api/ac/ack?id=%d&status=%s', senderUrl(), id, status), function() end)
  if detail then ac.log('LeaderboardSector: ' .. detail) end
end

-- A spot the car has actually driven, just before the start/finish line.
-- physics.teleportCarTo() jumps to the track's spawn set, which some tracks
-- either lack or place badly - the car then ends up under the map and AC
-- recovers it to the pits. physics.setCarPosition() is documented to align
-- the car on the track surface, so replaying a real on-track position is far
-- safer and puts the next driver exactly where we want them.
local gridPos, gridDir = nil, nil

local function rememberStartPoint()
  local car = ac.getCar(0)
  if not car then return end
  local sp = car.splinePosition
  -- just before the line, and actually moving along the track
  if sp and sp > 0.985 and sp < 0.999 and car.speedKmh and car.speedKmh > 5 then
    gridPos = vec3(car.position.x, car.position.y, car.position.z)
    gridDir = vec3(car.look.x, car.look.y, car.look.z)
  end
end

local function runCommand(body)
  local action, id, arg = body:match('^(%w+)|(%d+)|(%S+)$')
  id = tonumber(id)
  if action ~= 'teleport' or not id then return end
  if id <= lastExecutedId then return end  -- already handled, server just hasn't seen the ack yet
  lastExecutedId = id

  local usedRecalled = false
  local ok, err = pcall(function()
    if gridPos and gridDir and arg ~= 'PIT' then
      -- Surface-aligned, and a position we know is on the racing line
      physics.setCarPosition(0, gridPos, gridDir)
      usedRecalled = true
    else
      physics.teleportCarTo(0, arg)
    end
    -- Drop whatever speed the car carried into the failure, otherwise it
    -- arrives at the line still doing 150km/h and immediately crashes
    pcall(function() physics.setCarVelocity(0, vec3(0, 0, 0)) end)
  end)

  if ok then
    if usedRecalled then
      lastTeleportText = string.format('Moved to the start/finish line (command %d)', id)
    else
      lastTeleportText = string.format('Teleported to %s (command %d)', arg, id)
    end
    sendAck(id, true, lastTeleportText)
  else
    -- If the surface-aligned move failed for any reason, still try the spawn set
    local fallbackOk = pcall(function() physics.teleportCarTo(0, arg) end)
    lastTeleportText = fallbackOk
      and string.format('Used %s spawn after a failed move (command %d)', arg, id)
      or ('Teleport failed: ' .. tostring(err))
    sendAck(id, fallbackOk, lastTeleportText)
  end
end

-- Watch the car often enough to catch it passing the line
setInterval(function()
  pcall(rememberStartPoint)
end, 0.05)

setInterval(function()
  if requestPending then return end
  requestPending = true
  web.get(senderUrl() .. '/api/ac/command', function(err, response)
    requestPending = false
    local body = response and response.body or nil
    -- Only our endpoint answers 200 with 'none'/'teleport|...' - anything
    -- else (another app squatting the port) means: try the next port
    local looksRight = (not err) and response and response.status == 200
      and body and (body == 'none' or body:match('^%w+|%d+|%S+$'))
    if not looksRight then
      nextPort()
      statusOk = false
      statusText = 'Looking for Lap Time Sender...'
      return
    end
    connected = true
    statusOk = true
    statusText = 'Connected to Lap Time Sender (port ' .. SENDER_PORTS[portIndex] .. ')'
    if body ~= 'none' then
      runCommand(body)
    end
  end)
end, POLL_PERIOD)

function script.windowMain(dt)
  ui.pushFont(ui.Font.Title)
  ui.text('Leaderboard Sector')
  ui.popFont()
  ui.textColored(statusText, statusOk and rgbm(0.4, 1, 0.4, 1) or rgbm(1, 0.8, 0.3, 1))
  if gridPos then
    ui.textColored('Start point learned', rgbm(0.4, 1, 0.4, 1))
  else
    ui.textColored('Start point not learned yet - drive past the line once',
      rgbm(1, 0.8, 0.3, 1))
  end
  ui.text(lastTeleportText)
end
