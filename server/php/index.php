<?php
/**
 * Tidesight collector: entry point for Apache and other shared hosting (see .htaccess and README.md).
 *
 * Point it at the resolved configuration, kept OUTSIDE the web root (it contains the secret):
 * uncomment the define() below, or set the TIDESIGHT_CONFIG environment variable
 * (e.g. `SetEnv TIDESIGHT_CONFIG /home/USER/tidesight/tidesight.json` in .htaccess).
 * Without either, a tidesight.json next to this file is used.
 *
 * If tidesight.php and src/ live elsewhere (recommended: outside the web root too), change
 * the require below to their location.
 */

// define('TIDESIGHT_CONFIG', '/home/USER/tidesight/tidesight.json');

require __DIR__ . '/tidesight.php';

\Tidesight\run(__DIR__);
