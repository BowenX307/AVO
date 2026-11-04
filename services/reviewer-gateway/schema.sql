CREATE TABLE `budget` (
	`id` integer PRIMARY KEY NOT NULL,
	`used` integer DEFAULT 0 NOT NULL
);
--> statement-breakpoint
CREATE TABLE `reservations` (
	`id` text PRIMARY KEY NOT NULL,
	`amount` integer NOT NULL,
	`created` integer NOT NULL,
	`state` text NOT NULL
);
