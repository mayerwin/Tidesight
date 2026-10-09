<?php
/**
 * Tidesight collector: router for PHP's built-in web server (development, tests).
 *
 *     TIDESIGHT_CONFIG=/path/to/tidesight.json php -S 127.0.0.1:8080 server/php/router.php
 *
 * Every request goes to the collector: nothing is ever served from the document root.
 * Do not use the built-in server in production; see README.md for Apache / shared hosting.
 */

require __DIR__ . '/tidesight.php';

\Tidesight\run(__DIR__);

return true;
