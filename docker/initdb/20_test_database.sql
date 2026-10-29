-- A second, empty database for the automated tests, so tests never touch the data of real runs.
-- template_postgis is made by the PostGIS image and already holds the PostGIS extension.
CREATE DATABASE iris_test TEMPLATE template_postgis;
