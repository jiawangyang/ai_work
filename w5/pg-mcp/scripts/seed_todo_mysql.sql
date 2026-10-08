-- ============================================================================
-- Seed schema for the MySQL smoke-test database `todo_db`
-- Server: MySQL 5.7.44 — matches the DATABASES entry in .env (never commit it)
-- Run: mysql -h <host> -u root -p < scripts/seed_todo_mysql.sql
-- ============================================================================

CREATE DATABASE IF NOT EXISTS `todo_db`
  DEFAULT CHARACTER SET utf8mb4;

USE `todo_db`;

CREATE TABLE `users` (
  `id` int(11) NOT NULL AUTO_INCREMENT COMMENT 'User ID',
  `email` varchar(255) NOT NULL COMMENT 'Login email',
  `name` varchar(100) NOT NULL COMMENT 'Display name',
  `status` enum('active','inactive','banned') NOT NULL DEFAULT 'active' COMMENT 'Account status',
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP COMMENT 'Signup time',
  PRIMARY KEY (`id`),
  UNIQUE KEY `email` (`email`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Registered users';

CREATE TABLE `todos` (
  `id` int(11) NOT NULL AUTO_INCREMENT COMMENT 'Todo ID',
  `user_id` int(11) NOT NULL COMMENT 'Owner user ID',
  `title` varchar(200) NOT NULL COMMENT 'Todo title',
  `done` tinyint(1) NOT NULL DEFAULT '0' COMMENT 'Completed flag',
  `due_date` date DEFAULT NULL COMMENT 'Due date',
  PRIMARY KEY (`id`),
  KEY `idx_todos_user` (`user_id`),
  CONSTRAINT `fk_todos_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='User todo items';

-- Sample data (3 users / 3 todos)
INSERT INTO `users` (`email`, `name`, `status`) VALUES
  ('alice@example.com',   'Alice',   'active'),
  ('bob@example.com',     'Bob',     'inactive'),
  ('carol@example.com',   'Carol',   'active');

INSERT INTO `todos` (`user_id`, `title`, `done`, `due_date`) VALUES
  (1, 'Write report',  0, '2026-10-05'),
  (1, 'Review PR',     1, NULL),
  (3, 'Plan sprint',   0, '2026-10-12');
