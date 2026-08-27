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

local function runCommand(body)
  local action, id, arg = body:match('^(%w+)|(%d+)|(%S+)$')
  id = tonumber(id)
  if action ~= 'teleport' or not id then return end
  if id <= lastExecutedId then return end  -- already handled, server just hasn't seen the ack yet
  lastExecutedId = id

  local ok, err = pcall(function()
    physics.teleportCarTo(0, arg)
  end)
  if ok then
    lastTeleportText = string.format('Teleported to %s (command %d)', arg, id)
    sendAck(id, true, lastTeleportText)
  else
    lastTeleportText = 'Teleport failed: ' .. tostring(err)
    sendAck(id, false, lastTeleportText)
  end
end

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
  ui.text(lastTeleportText)
end
