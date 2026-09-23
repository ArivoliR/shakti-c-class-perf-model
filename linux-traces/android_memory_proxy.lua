-- Structural proxy for Android framework code running on ART.
-- This is proxy code, not ART: metatable dispatch stands in for virtual calls;
-- tables/strings/object churn stand in for framework maps, Binder marshalling,
-- allocation, and GC activity.
local View = {}
View.__index = View
function View.new(id, w, h)
  return setmetatable({id=id, w=w, h=h, children={}, props={}}, View)
end
function View:measure() return self.w * self.h end
function View:addChild(child) self.children[#self.children+1] = child; return self end
function View:setProp(key, value) self.props[key] = value end
function View:layout(depth)
  local area = self:measure()
  for i=1,#self.children do area = area + self.children[i]:layout(depth+1) end
  return area
end

local Button = setmetatable({}, {__index = View})
Button.__index = Button
function Button.new(id, w, h)
  local object = View.new(id, w, h)
  return setmetatable(object, Button)
end
function Button:measure() return self.w * self.h + 4 end

local registry, total = {}, 0
for round = 1, 2 do
  local root = View.new("root", 32, 32)
  for i = 1, 300 do
    local node = (i % 3 == 0) and Button.new("b"..i, i%17+1, i%13+1)
                                  or View.new("v"..i, i%11+1, i%7+1)
    node:setProp("key-"..(i%50), "value-"..i)
    root:addChild(node)
    registry["obj-"..round.."-"..i] = node
  end
  total = total + root:layout(0)
  local parts = {}
  for i = 1, 200 do
    parts[#parts+1] = string.format("%s=%d;", "f"..i, i*7)
  end
  local blob = table.concat(parts)
  for key, value in string.gmatch(blob, "(%w+)=(%d+);") do
    total = total + #key + #value
  end
  if round % 4 == 0 then
    for key in pairs(registry) do
      if math.random() < 0.5 then registry[key] = nil end
    end
    collectgarbage("step")
  end
end
print("framework_total", total)

-- End with interpreter-to-native calls that walk a deterministic 2 MiB ring.
-- cache_pressure is supplied by lua_proxy_driver.c; each call makes eight
-- dependent, pseudo-random cache-line accesses.
local index = 0
for i = 1, 4000 do
  local value
  index, value = cache_pressure(index)
  total = total + value
  if i % 64 == 0 then
    registry["walk-"..(i % 1024)] = string.format("%x:%d", value, index)
  end
end
