// wifiinfo: prints the current Wi-Fi link as one JSON line.
// Built by `netscan.py install` (swiftc -O wifiinfo.swift -o bin/wifiinfo).
// CoreWLAN gives signal/noise/rate/channel without any permission; ssid/bssid
// come back nil unless the calling app has Location Services access, so
// netscan falls back to `ipconfig getsummary` and the router MAC for identity.
import CoreWLAN
import Foundation

var d: [String: Any] = [:]
if let i = CWWiFiClient.shared().interface() {
    d["iface"] = i.interfaceName ?? ""
    d["power"] = i.powerOn()
    d["ssid"] = i.ssid() ?? NSNull()
    d["bssid"] = i.bssid() ?? NSNull()
    d["rssi"] = i.rssiValue()
    d["noise"] = i.noiseMeasurement()
    d["tx_rate"] = i.transmitRate()
    if let ch = i.wlanChannel() {
        d["channel"] = ch.channelNumber
        d["band"] = ["unknown", "2.4GHz", "5GHz", "6GHz"][min(ch.channelBand.rawValue, 3)]
        d["width"] = [0, 20, 40, 80, 160][min(ch.channelWidth.rawValue, 4)]
    }
    let phy = ["none", "802.11a", "802.11b", "802.11g", "802.11n", "802.11ac", "802.11ax", "802.11be"]
    let p = i.activePHYMode().rawValue
    d["phy"] = p < phy.count ? phy[p] : "phy\(p)"
}
let j = try! JSONSerialization.data(withJSONObject: d)
print(String(data: j, encoding: .utf8)!)
