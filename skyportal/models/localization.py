__all__ = [
    'Localization',
    'LocalizationTag',
    'LocalizationProperty',
    'LocalizationTile',
]

import sqlalchemy as sa
from sqlalchemy.orm import relationship, deferred
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.hybrid import hybrid_property

from astropy.table import Table
import dustmaps.sfd
from dustmaps.config import config
import numpy as np
import ligo.skymap.postprocess
import ligo.skymap.bayestar as ligo_bayestar
import healpy
import healpix_alchemy

from baselayer.app.models import Base, AccessibleIfUserMatches
from baselayer.app.env import load_env


_, cfg = load_env()
config['data_dir'] = cfg['misc.dustmap_folder']


class Localization(Base):
    """Localization information, including the localization ID, event ID, right
    ascension, declination, error radius (if applicable), and the healpix
    map. The healpix map is a multi-order healpix skymap, and this
    representation of the skymap has many tiles (in the
    LocalizationTile table). Healpix decomposes the sky into a set of equal
    area tiles each with a unique index, convenient for decomposing
    the sphere into subdivisions."""

    update = delete = AccessibleIfUserMatches('sent_by')

    sent_by_id = sa.Column(
        sa.ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc="The ID of the User who created this Localization.",
    )

    sent_by = relationship(
        "User",
        foreign_keys=sent_by_id,
        back_populates="localizations",
        doc="The user that saved this Localization",
    )

    nside = 512
    # HEALPix resolution used for flat (non-multiresolution) operations.

    dateobs = sa.Column(
        sa.ForeignKey('gcnevents.dateobs', ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc='UTC event timestamp',
    )

    localization_name = sa.Column(sa.String, doc='Localization name', index=True)

    uniq = deferred(
        sa.Column(
            sa.ARRAY(sa.BigInteger),
            nullable=False,
            doc='Multiresolution HEALPix UNIQ pixel index array',
        )
    )

    probdensity = deferred(
        sa.Column(
            sa.ARRAY(sa.Float),
            nullable=False,
            doc='Multiresolution HEALPix probability density array',
        )
    )

    distmu = deferred(
        sa.Column(sa.ARRAY(sa.Float), doc='Multiresolution HEALPix distance mu array')
    )

    distsigma = deferred(
        sa.Column(
            sa.ARRAY(sa.Float), doc='Multiresolution HEALPix distance sigma array'
        )
    )

    distnorm = deferred(
        sa.Column(
            sa.ARRAY(sa.Float),
            doc='Multiresolution HEALPix distance normalization array',
        )
    )

    contour = deferred(sa.Column(JSONB, doc='GeoJSON contours'))

    observationplan_requests = relationship(
        'ObservationPlanRequest',
        back_populates='localization',
        cascade='delete',
        passive_deletes=True,
        doc="Observation plan requests of the localization.",
    )

    survey_efficiency_analyses = relationship(
        'SurveyEfficiencyForObservations',
        back_populates='localization',
        cascade='delete',
        passive_deletes=True,
        doc="Survey efficiency analyses of the event.",
    )

    properties = relationship(
        'LocalizationProperty',
        cascade='save-update, merge, refresh-expire, expunge, delete',
        passive_deletes=True,
        order_by="LocalizationProperty.created_at",
        doc="Properties associated with this Localization.",
    )

    tags = relationship(
        'LocalizationTag',
        cascade='save-update, merge, refresh-expire, expunge, delete',
        passive_deletes=True,
        order_by="LocalizationTag.created_at",
        doc="Tags associated with this Localization.",
    )

    @hybrid_property
    def is_3d(self):
        return (
            self.distmu is not None
            and self.distsigma is not None
            and self.distnorm is not None
        )

    @is_3d.expression
    def is_3d(cls):
        return sa.and_(
            cls.distmu.isnot(None),
            cls.distsigma.isnot(None),
            cls.distnorm.isnot(None),
        )

    @property
    def table_2d(self):
        """Get multiresolution HEALPix dataset, probability density only."""
        return Table(
            [np.asarray(self.uniq, dtype=np.int64), self.probdensity],
            names=['UNIQ', 'PROBDENSITY'],
        )

    @property
    def table(self):
        """Get multiresolution HEALPix dataset, probability density and
        distance."""
        if self.is_3d:
            return Table(
                [
                    np.asarray(self.uniq, dtype=np.int64),
                    self.probdensity,
                    self.distmu,
                    self.distsigma,
                    self.distnorm,
                ],
                names=['UNIQ', 'PROBDENSITY', 'DISTMU', 'DISTSIGMA', 'DISTNORM'],
            )
        else:
            return self.table_2d

    @property
    def flat_2d(self):
        """Get flat resolution HEALPix dataset, probability density only."""
        order = healpy.nside2order(Localization.nside)
        result = ligo_bayestar.rasterize(self.table_2d, order)['PROB']
        return healpy.reorder(result, 'NESTED', 'RING')

    @property
    def flat(self):
        """Get flat resolution HEALPix dataset, probability density and
        distance."""
        if self.is_3d:
            order = healpy.nside2order(Localization.nside)
            t = ligo_bayestar.rasterize(self.table, order)
            result = t['PROB'], t['DISTMU'], t['DISTSIGMA'], t['DISTNORM']
            return healpy.reorder(result, 'NESTED', 'RING')
        else:
            return (self.flat_2d,)

    @property
    def center(self):
        """Get information about the center of the localization."""

        prob = self.flat_2d
        coord = ligo.skymap.postprocess.posterior_max(prob)

        center_info = {}
        center_info["ra"] = coord.ra.deg
        center_info["dec"] = coord.dec.deg
        center_info["gal_lat"] = coord.galactic.b.deg
        center_info["gal_lon"] = coord.galactic.l.deg

        try:
            ebv = float(dustmaps.sfd.SFDQuery()(coord))
        except Exception:
            ebv = None
        center_info["ebv"] = ebv

        return center_info


class LocalizationTile(Base):
    """This is a single tile within a skymap (as in the Localization table).
    Each tile has an associated healpix id and probability density."""

    localization_id = sa.Column(
        sa.ForeignKey('localizations.id', ondelete="CASCADE"),
        primary_key=True,
        index=True,
        doc='localization ID',
    )

    probdensity = sa.Column(
        sa.Float,
        nullable=False,
        index=True,
        doc="Probability density for the tile",
    )

    healpix = sa.Column(healpix_alchemy.Tile, primary_key=True, index=True)


LocalizationTile.__table_args__ = (
    sa.Index(
        'localizationtile_id_healpix_index',
        LocalizationTile.id,
        LocalizationTile.healpix,
        unique=True,
    ),
)


class LocalizationProperty(Base):
    """Store properties for localizations."""

    update = delete = AccessibleIfUserMatches('sent_by')

    sent_by_id = sa.Column(
        sa.ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc="The ID of the User who created this LocalizationProperty.",
    )

    sent_by = relationship(
        "User",
        foreign_keys=sent_by_id,
        back_populates="localizationproperties",
        doc="The user that saved this LocalizationProperty",
    )

    localization_id = sa.Column(
        sa.ForeignKey('localizations.id', ondelete="CASCADE"),
        primary_key=True,
        index=True,
        doc='localization ID',
    )

    data = sa.Column(JSONB, doc="Localization properties in JSON format.", index=True)


class LocalizationTag(Base):
    """Store qualitative tags for localizations."""

    update = delete = AccessibleIfUserMatches('sent_by')

    sent_by_id = sa.Column(
        sa.ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc="The ID of the User who created this LocalizationTag.",
    )

    sent_by = relationship(
        "User",
        foreign_keys=sent_by_id,
        back_populates="localizationtags",
        doc="The user that saved this LocalizationTag",
    )

    localization_id = sa.Column(
        sa.ForeignKey('localizations.id', ondelete="CASCADE"),
        primary_key=True,
        index=True,
        doc='localization ID',
    )

    text = sa.Column(sa.Unicode, nullable=False, index=True)
