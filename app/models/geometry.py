from geoalchemy2 import Geometry, WKBElement


class LocalMapGeometry(Geometry):
    """Decode SRID-0 WKB without mistaking the ring count for an SRID.

    PostGIS omits the SRID flag for local coordinates. GeoAlchemy2 0.20.0
    forces extended WKB decoding, so detect the flag from the actual payload.
    """

    cache_ok = True

    def result_processor(self, dialect, coltype):
        def process(value):
            if value is None:
                return None
            # Normalize to EWKB so re-binding does not require Shapely's WKT conversion.
            return WKBElement(value, srid=self.srid, extended=None).as_ewkb()

        return process
