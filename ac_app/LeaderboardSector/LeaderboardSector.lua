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

local SENDER_URL = 'http://127.0.0.1:5001'
local POLL_PERIOD = 0.25

local lastExecutedId = 0
local requestPending = false
local statusText = 'Looking for Lap Time Sender...'
local statusOk = false
local lastTeleportText = 'No teleports yet'

local function sendAck(id, ok, detail)
  local status = ok and 'ok' or 'error'
  web.get(string.format('%s/api/ac/ack?id=%d&status=%s', SENDER_URL, id, status), function() end)
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
  web.get(SENDER_URL .. '/api/ac/command', function(err, response)
    requestPending = false
    if err or not response or response.status ~= 200 then
      statusOk = false
      statusText = 'Looking for Lap Time Sender...'
      return
    end
    statusOk = true
    statusText = 'Connected to Lap Time Sender'
    local body = response.body
    if body and body ~= '' and body ~= 'none' then
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
