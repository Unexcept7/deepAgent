CREATE DATABASE IF NOT EXISTS enterprise_mcp
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE enterprise_mcp;

CREATE TABLE IF NOT EXISTS devices (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  device_id VARCHAR(64) NOT NULL,
  model VARCHAR(64) NOT NULL,
  device_name VARCHAR(128) NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'offline',
  ai_channels INT UNSIGNED NULL,
  di_channels INT UNSIGNED NULL,
  do_channels INT UNSIGNED NULL,
  protocol VARCHAR(64) NULL,
  description TEXT NULL,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_devices_device_id (device_id),
  KEY idx_devices_model (model),
  KEY idx_devices_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS alarms (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  alarm_id VARCHAR(64) NOT NULL,
  device_id VARCHAR(64) NOT NULL,
  level VARCHAR(16) NOT NULL,
  message VARCHAR(255) NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_alarms_alarm_id (alarm_id),
  KEY idx_alarms_device_time (device_id, created_at),
  CONSTRAINT fk_alarms_device_id FOREIGN KEY (device_id)
    REFERENCES devices (device_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
