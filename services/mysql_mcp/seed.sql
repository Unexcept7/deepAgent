USE enterprise_mcp;

START TRANSACTION;

INSERT INTO devices (
  device_id, model, device_name, status, ai_channels, di_channels, do_channels,
  protocol, description, updated_at
) VALUES
  ('DEV-M1101-01', 'M1101', '现场 DI 模块-01', 'online',  NULL, 16, NULL, 'Modbus RTU',  '16 路数字量输入；联调数据，产品参数以原始手册为准。', '2026-10-15 08:20:00'),
  ('DEV-M1201-01', 'M1201', '现场 DO 模块-01', 'online',  NULL, NULL, 16, 'Modbus RTU',  '16 路数字量输出；联调数据，产品参数以原始手册为准。', '2026-10-15 08:18:00'),
  ('DEV-M1304-01', 'M1304', '温度采集模块-01', 'warning', 4, NULL, NULL, 'Modbus RTU',  '4 路温度输入；联调数据，产品参数以原始手册为准。', '2026-10-15 08:15:00'),
  ('DEV-M1401-01', 'M1401', '模拟量输出模块-01', 'offline', NULL, NULL, 4, 'Modbus RTU',  '模拟量输出联调记录；具体规格以原始文档为准。', '2026-10-15 08:10:00'),
  ('DEV-M1503-01', 'M1503', '混合 IO 模块-01', 'online',  4, 16, 4, 'Modbus RTU',  '4 路 AI、16 路 DI、4 路 DO；联调数据，需与原始手册核对。', '2026-10-15 08:08:00'),
  ('DEV-M1507-01', 'M1507', '混合 IO 模块-02', 'warning', 4, 4, 4, 'Modbus RTU',  '通道组合联调记录；DI 配置待原始资料确认。', '2026-10-15 08:05:00'),
  ('DEV-M1516-01', 'M1516', '数字量扩展模块-01', 'online', NULL, 8, 8, 'Modbus TCP', '8 路数字量输入、8 路数字量输出；联调数据。', '2026-10-15 08:02:00'),
  ('DEV-M1101-02', 'M1101', '设备箱 DI 模块-02', 'online', NULL, 16, NULL, 'RS485',      '16 路数字量输入；第二个现场实例。', '2026-10-14 17:40:00'),
  ('DEV-M1201-02', 'M1201', '设备箱 DO 模块-02', 'offline', NULL, NULL, 16, 'RS485',      '16 路数字量输出；第二个现场实例。', '2026-10-14 17:35:00'),
  ('DEV-M1304-02', 'M1304', '温度采集模块-02', 'online',  4, NULL, NULL, 'RS485',      '4 路温度输入；第二个现场实例。', '2026-10-14 17:30:00'),
  ('DEV-M1401-02', 'M1401', '模拟量输出模块-02', 'warning', NULL, NULL, 4, 'RS485',      '模拟量输出联调记录；第二个现场实例。', '2026-10-14 17:25:00'),
  ('DEV-M1503-02', 'M1503', '混合 IO 模块-03', 'online',  4, 16, 4, 'RS485',      '4 路 AI、16 路 DI、4 路 DO；第二个现场实例。', '2026-10-14 17:20:00'),
  ('DEV-M1507-02', 'M1507', '混合 IO 模块-04', 'offline', 4, 4, 4, 'RS485',      'DI 配置待原始资料确认；第二个现场实例。', '2026-10-14 17:15:00'),
  ('DEV-M1516-02', 'M1516', '数字量扩展模块-02', 'warning', NULL, 8, 8, 'Modbus TCP', '8 路数字量输入、8 路数字量输出；第二个现场实例。', '2026-10-14 17:10:00'),
  ('DEV-PLC-001',   'PLC-X100', '产线控制器-01', 'online',  8, 16, 16, 'Modbus TCP', '产线控制器，包含 AI、DI、DO 通道。', '2026-10-14 16:50:00'),
  ('DEV-PLC-002',   'PLC-X200', '产线控制器-02', 'online', 16, 32, 24, 'Modbus TCP', '产线控制器，包含 AI、DI、DO 通道。', '2026-10-14 16:45:00'),
  ('DEV-RTU-001',   'RTU-A01', '远程 IO 站-01', 'offline', 8, 16, 8, 'Modbus RTU', '远程 IO 站，现场总线接入。', '2026-10-14 16:40:00'),
  ('DEV-RTU-002',   'RTU-A02', '远程 IO 站-02', 'warning', 8, 8, 8, 'Modbus RTU', '远程 IO 站，现场总线接入。', '2026-10-14 16:35:00'),
  ('DEV-SENSOR-001', 'TEMP-P100', '温度传感器网关-01', 'online', 8, NULL, NULL, 'MQTT', '温度传感器网关，上传采集数据。', '2026-10-14 16:30:00'),
  ('DEV-SENSOR-002', 'PRESS-P200', '压力传感器网关-01', 'online', 8, NULL, NULL, 'MQTT', '压力传感器网关，上传采集数据。', '2026-10-14 16:25:00')
ON DUPLICATE KEY UPDATE
  model = VALUES(model),
  device_name = VALUES(device_name),
  status = VALUES(status),
  ai_channels = VALUES(ai_channels),
  di_channels = VALUES(di_channels),
  do_channels = VALUES(do_channels),
  protocol = VALUES(protocol),
  description = VALUES(description),
  updated_at = VALUES(updated_at);

INSERT INTO alarms (alarm_id, device_id, level, message, created_at) VALUES
  ('ALM-20261015-0001', 'DEV-M1101-01', 'warning',  'DI 通道 05 输入状态抖动。', '2026-10-15 08:25:00'),
  ('ALM-20261015-0002', 'DEV-M1201-01', 'info',     'DO 模块完成周期性状态上报。', '2026-10-15 08:22:00'),
  ('ALM-20261015-0003', 'DEV-M1304-01', 'critical', '通道 02 温度采集超出配置范围。', '2026-10-15 08:17:00'),
  ('ALM-20261015-0004', 'DEV-M1401-01', 'critical', '连续三个周期未收到输出模块响应。', '2026-10-15 08:12:00'),
  ('ALM-20261015-0005', 'DEV-M1503-01', 'warning',  'DI 通道 11 检测到异常变化。', '2026-10-15 08:09:00'),
  ('ALM-20261015-0006', 'DEV-M1507-01', 'warning',  '设备通道配置与当前任务模板不一致。', '2026-10-15 08:06:00'),
  ('ALM-20261015-0007', 'DEV-M1516-01', 'info',     '数字量扩展模块完成重连。', '2026-10-15 08:03:00'),
  ('ALM-20261014-0008', 'DEV-M1101-02', 'warning',  'RS485 总线出现一次通信重试。', '2026-10-14 17:42:00'),
  ('ALM-20261014-0009', 'DEV-M1201-02', 'critical', 'DO 模块离线，未收到心跳。', '2026-10-14 17:38:00'),
  ('ALM-20261014-0010', 'DEV-M1304-02', 'info',     '温度输入模块恢复在线。', '2026-10-14 17:32:00'),
  ('ALM-20261014-0011', 'DEV-M1401-02', 'warning',  '输出通道 03 电流反馈偏低。', '2026-10-14 17:28:00'),
  ('ALM-20261014-0012', 'DEV-M1503-02', 'info',     '设备参数读取成功。', '2026-10-14 17:23:00'),
  ('ALM-20261014-0013', 'DEV-M1507-02', 'critical', '设备离线超过通信超时阈值。', '2026-10-14 17:18:00'),
  ('ALM-20261014-0014', 'DEV-M1516-02', 'warning',  '输出组 01 状态与期望值不一致。', '2026-10-14 17:12:00'),
  ('ALM-20261014-0015', 'DEV-PLC-001',   'info',     '产线控制器完成配置同步。', '2026-10-14 16:55:00'),
  ('ALM-20261014-0016', 'DEV-PLC-002',   'warning',  'DI 输入组 02 存在连续高频变化。', '2026-10-14 16:50:00'),
  ('ALM-20261014-0017', 'DEV-RTU-001',   'critical', '远程 IO 站连接断开。', '2026-10-14 16:44:00'),
  ('ALM-20261014-0018', 'DEV-RTU-002',   'warning',  '远程 IO 站通信延迟升高。', '2026-10-14 16:39:00'),
  ('ALM-20261014-0019', 'DEV-SENSOR-001', 'info',    '温度网关完成数据批量上报。', '2026-10-14 16:34:00'),
  ('ALM-20261014-0020', 'DEV-SENSOR-002', 'warning',  '压力传感器网关出现一条格式异常数据。', '2026-10-14 16:29:00')
ON DUPLICATE KEY UPDATE
  device_id = VALUES(device_id),
  level = VALUES(level),
  message = VALUES(message),
  created_at = VALUES(created_at);

COMMIT;
