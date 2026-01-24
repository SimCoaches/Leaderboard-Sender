-- Leaderboard Sender v5.3 - Extract all laps from mission end
local M = {}

-- Configuration
local SERVER_PORT = 5002  -- UDP port for lap data
local SERVER_HOST = "127.0.0.1"

-- UDP socket
local socket = nil
local udp = nil

-- Track state to avoid duplicate lap submissions
local lastLapTime = 0
local lastLapTimestamp = 0
local lastCumulativeTime = 0  -- For quickrace cumulative time tracking
local sentLapCount = 0  -- Track how many laps we've already sent in this session
local currentMissionLapsSent = 0  -- Track laps sent in current mission run

-- Initialize UDP socket
local function initSocket()
    if udp then return true end
    
    local success, err = pcall(function()
        socket = require("socket")
        udp = socket.udp()
        udp:settimeout(0)  -- Non-blocking
        log("I", "LeaderboardSender", string.format("UDP socket initialized for %s:%d", SERVER_HOST, SERVER_PORT))
    end)
    
    if not success then
        log("W", "LeaderboardSender", "Failed to initialize UDP socket: " .. tostring(err))
        return false
    end
    return true
end

-- Get current vehicle and track info from game state
local function getCurrentContext()
    local vehicle = "unknown"
    local track = "unknown"
    
    -- Get vehicle name
    local playerVehicle = be:getPlayerVehicle(0)
    if playerVehicle then
        vehicle = playerVehicle:getJBeamFilename() or "unknown"
    end
    
    -- Get current level/track name
    local levelPath = getMissionFilename()
    if levelPath then
        track = string.match(levelPath, "([^/]+)/[^/]+%.json$") or levelPath
    end
    
    return vehicle, track
end

-- Send lap data via UDP (instant!)
local function sendLapUDP(lapTime, vehicle, track, valid)
    if not initSocket() then
        log("W", "LeaderboardSender", "Cannot send lap - no socket")
        return false
    end
    
    -- Format: LAP|time|vehicle|track|valid|timestamp
    local message = string.format("LAP|%.3f|%s|%s|%s|%d", 
        lapTime, 
        vehicle, 
        track, 
        valid and "1" or "0",
        os.time())
    
    local success, err = udp:sendto(message, SERVER_HOST, SERVER_PORT)
    if success then
        log("I", "LeaderboardSender", string.format(">>> UDP SENT: %.3fs on %s", lapTime, track))
        return true
    else
        log("W", "LeaderboardSender", "UDP send failed: " .. tostring(err))
        return false
    end
end

-- METHOD 1: For quickrace scenarios with waypoint system
local function onRaceWaypointReached(data)
    if not data then return end
    
    local cumulativeTime = data.lapTime or data.time
    if not cumulativeTime or cumulativeTime <= 0 then return end
    
    -- Only process finish line waypoints
    local waypointName = data.waypointName or ""
    if not string.find(waypointName:lower(), "finish") then
        return
    end
    
    -- Calculate individual lap time
    local individualLapTime = cumulativeTime
    if lastCumulativeTime > 0 then
        individualLapTime = cumulativeTime - lastCumulativeTime
    end
    lastCumulativeTime = cumulativeTime
    
    -- Avoid duplicates
    local currentTime = os.time()
    if math.abs(individualLapTime - lastLapTime) < 0.1 and (currentTime - lastLapTimestamp) < 1 then
        return
    end
    
    lastLapTime = individualLapTime
    lastLapTimestamp = currentTime
    sentLapCount = sentLapCount + 1
    currentMissionLapsSent = currentMissionLapsSent + 1
    
    local vehicle, track = getCurrentContext()
    log("I", "LeaderboardSender", string.format("[QUICKRACE] Lap %d: %.3fs", sentLapCount, individualLapTime))
    sendLapUDP(individualLapTime, data.vehicleName or vehicle, data.trackName or track, data.valid ~= false)
end

-- Recursively search for historicTimes in a table structure
local function findHistoricTimes(tbl, depth)
    if not tbl or type(tbl) ~= "table" or depth > 10 then
        return nil
    end
    
    -- Direct check
    if tbl.historicTimes then
        return tbl.historicTimes
    end
    
    -- Search in known paths
    if tbl.race and tbl.race.states then
        for vehId, state in pairs(tbl.race.states) do
            if state.historicTimes then
                return state.historicTimes
            end
        end
    end
    
    if tbl.raceData and tbl.raceData.states then
        for vehId, state in pairs(tbl.raceData.states) do
            if state.historicTimes then
                return state.historicTimes
            end
        end
    end
    
    -- Recursive search (limited depth)
    for k, v in pairs(tbl) do
        if type(v) == "table" and k ~= "parent" and k ~= "__index" then
            local result = findHistoricTimes(v, depth + 1)
            if result then
                return result
            end
        end
    end
    
    return nil
end

-- METHOD 2: Mission completion - extract ALL laps from mission data
local function onMissionAttemptAggregated(attempt, mission, progressKey)
    if not attempt then return end
    
    local missionId = mission and mission.id or "unknown"
    log("I", "LeaderboardSender", string.format("[MISSION END] %s", missionId))
    
    local vehicle, track = getCurrentContext()
    local lapsSentThisRun = 0
    
    -- Strategy 1: Try to find historicTimes in the attempt data
    local historicTimes = findHistoricTimes(attempt, 0)
    
    if historicTimes and type(historicTimes) == "table" then
        log("I", "LeaderboardSender", string.format("[MISSION END] Found %d laps in historicTimes", #historicTimes))
        
        for i, lapData in ipairs(historicTimes) do
            if lapData and lapData.lapTime and lapData.lapTime > 0 then
                log("I", "LeaderboardSender", string.format("[MISSION END] Lap %d: %.3fs", i, lapData.lapTime))
                sendLapUDP(lapData.lapTime, vehicle, track, true)
                lapsSentThisRun = lapsSentThisRun + 1
            end
        end
    else
        log("W", "LeaderboardSender", "[MISSION END] Could not find historicTimes in attempt data")
        
        -- Strategy 2: Fallback to bestLap only if we have it
        if attempt.data and attempt.data.bestLap and attempt.data.bestLap > 0 then
            log("I", "LeaderboardSender", string.format("[MISSION END] Sending best lap only: %.3fs", attempt.data.bestLap))
            sendLapUDP(attempt.data.bestLap, vehicle, track, true)
            lapsSentThisRun = 1
        end
    end
    
    sentLapCount = sentLapCount + lapsSentThisRun
    log("I", "LeaderboardSender", string.format("[MISSION END] Sent %d laps from this run (total session: %d)", lapsSentThisRun, sentLapCount))
    
    -- Reset for next mission
    currentMissionLapsSent = 0
end

local function onScenarioChange(scenario)
    if scenario then
        log("I", "LeaderboardSender", string.format("Scenario: %s", scenario.name or "unknown"))
    end
    -- Reset state for new scenario/mission
    lastCumulativeTime = 0
    currentMissionLapsSent = 0
    lastLapTime = 0
    lastLapTimestamp = 0
end

local function onExtensionLoaded()
    initSocket()
    log("I", "LeaderboardSender", "v5.3 LOADED - Extracts laps from mission end")
end

-- Heartbeat
local heartbeatTimer = 0
local function onUpdate(dtReal, dtSim, dtRaw)
    heartbeatTimer = heartbeatTimer + dtReal
    if heartbeatTimer >= 60 then
        heartbeatTimer = 0
        log("I", "LeaderboardSender", string.format("Active - sent %d laps total", sentLapCount))
    end
end

M.onExtensionLoaded = onExtensionLoaded
M.onRaceWaypointReached = onRaceWaypointReached
M.onMissionAttemptAggregated = onMissionAttemptAggregated
M.onScenarioChange = onScenarioChange
M.onUpdate = onUpdate

return M
