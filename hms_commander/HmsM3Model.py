"""
HmsM3Model - Manage HCFCD M3 Model HMS projects

This module provides access to HEC-HMS projects within the Harris County Flood
Control District (HCFCD) M3 Models - Current FEMA effective H&H models.

M3 Models contain both HEC-RAS (hydraulic) and HEC-HMS (hydrologic) models.
This class focuses on extracting and managing the HMS portion.

Key Features:
- List available HMS projects across all 22 M3 model watersheds
- Extract specific HMS projects for use with hms-commander
- Query by model ID, unit ID, or channel name
- Integrates with M3Model from ras-commander (if available)

Usage:
    from hms_commander import HmsM3Model

    # List all HMS projects
    projects = HmsM3Model.list_projects()

    # Extract a specific project
    path = HmsM3Model.extract_project('D', 'D100-00-00')

    # Find project by channel name
    model_id, unit_id = HmsM3Model.get_project_by_channel('BRAYS BAYOU')

Note:
    All M3 HMS projects use HMS 3.x format, which requires:
    - python2_compatible=True for Jython script generation
    - HmsJython compatibility for execution

See Also:
    - M3Model (ras-commander): For HEC-RAS project access
    - HmsExamples: For HMS installation example projects
"""

import pandas as pd
from pathlib import Path
import shutil
from typing import Union, List, Dict, Optional, Tuple

from .LoggingConfig import get_logger, log_call
from ._m3_intake import extract_project as _extract_m3_project

logger = get_logger(__name__)


class HmsM3Model:
    """
    Manage HEC-HMS projects from HCFCD M3 Models.

    M3 Models are Harris County Flood Control District's FEMA effective H&H
    models for major bayous and watersheds in the Houston, Texas region.

    This class provides HMS-specific access to these models, complementing
    the M3Model class in ras-commander which handles the RAS portion.

    All methods are class methods - no instantiation required.

    Example:
        # List available HMS projects
        projects = HmsM3Model.list_projects()

        # Get projects for Brays Bayou (Model D)
        brays = HmsM3Model.list_projects(model_id='D')

        # Extract for use with hms-commander
        path = HmsM3Model.extract_project('D', 'D100-00-00')
        hms = init_hms_project(path)
    """

    # Base URL for M3 Model downloads
    base_url = 'https://files.m3models.org/modellibrary/'

    # Base directory for model storage
    base_dir = Path.cwd()
    models_dir = base_dir / 'm3_hms_projects'

    # Model metadata - mirrors M3Model from ras-commander
    MODELS = {
        'A': {
            'name': 'Clear Creek',
            'short_name': 'Clear',
            'effective_date': '2022-05-05',
            'size_gb': 0.03,
            'primary_channels': ['CLEAR CREEK']
        },
        'B': {
            'name': 'Armand Bayou',
            'short_name': 'Armand',
            'effective_date': '2022-05-05',
            'size_gb': 0.04,
            'primary_channels': ['ARMAND BAYOU']
        },
        'C': {
            'name': 'Sims Bayou',
            'short_name': 'Sims',
            'effective_date': '2022-05-05',
            'size_gb': 0.01,
            'primary_channels': ['SIMS BAYOU']
        },
        'D': {
            'name': 'Brays Bayou',
            'short_name': 'Brays',
            'effective_date': '2022-05-05',
            'size_gb': 0.03,
            'primary_channels': ['BRAYS BAYOU']
        },
        'E': {
            'name': 'White Oak Bayou',
            'short_name': 'WhiteOak',
            'effective_date': '2023-01-30',
            'size_gb': 0.02,
            'primary_channels': ['WHITE OAK BAYOU']
        },
        'F': {
            'name': 'San Jacinto/Galveston Bay',
            'short_name': 'GalvBay',
            'effective_date': '2022-05-05',
            'size_gb': 0.01,
            'primary_channels': ['BAYPORT CHANNEL', 'SHIP CHANNEL']
        },
        'G': {
            'name': 'San Jacinto River',
            'short_name': 'SanJac',
            'effective_date': '2022-05-05',
            'size_gb': 0.09,
            'primary_channels': ['SAN JACINTO RIVER', 'EAST FORK SAN JACINTO RIVER']
        },
        'H': {
            'name': 'Hunting Bayou',
            'short_name': 'Hunting',
            'effective_date': '2022-05-05',
            'size_gb': 0.01,
            'primary_channels': ['HUNTING BAYOU']
        },
        'I': {
            'name': 'Vince Bayou',
            'short_name': 'Vince',
            'effective_date': '2022-05-05',
            'size_gb': 0.01,
            'primary_channels': ['VINCE BAYOU', 'LITTLE VINCE BAYOU']
        },
        'J': {
            'name': 'Spring Creek',
            'short_name': 'Spring',
            'effective_date': '2022-05-05',
            'size_gb': 0.06,
            'primary_channels': ['SPRING CREEK', 'SPRING BRANCH']
        },
        'K': {
            'name': 'Cypress Creek',
            'short_name': 'Cypress',
            'effective_date': '2022-05-05',
            'size_gb': 0.04,
            'primary_channels': ['CYPRESS CREEK']
        },
        'L': {
            'name': 'Little Cypress Creek',
            'short_name': 'LttlCyp',
            'effective_date': '2022-05-05',
            'size_gb': 0.03,
            'primary_channels': ['LITTLE CYPRESS CREEK']
        },
        'M': {
            'name': 'Willow Creek',
            'short_name': 'Willow',
            'effective_date': '2023-01-30',
            'size_gb': 0.05,
            'primary_channels': ['WILLOW CREEK', 'WILLOW WATER HOLE']
        },
        'N': {
            'name': 'Carpenters Bayou',
            'short_name': 'Carpenters',
            'effective_date': '2022-05-05',
            'size_gb': 0.01,
            'primary_channels': ['CARPENTERS BAYOU']
        },
        'O': {
            'name': 'Spring Gully and Goose Creek',
            'short_name': 'SprgGully',
            'effective_date': '2022-05-05',
            'size_gb': 0.01,
            'primary_channels': ['SPRING GULLY', 'GOOSE CREEK', 'E. FORK GOOSE CREEK', 'W. FORK GOOSE CREEK']
        },
        'P': {
            'name': 'Greens Bayou',
            'short_name': 'Greens',
            'effective_date': '2024-03-04',
            'size_gb': 0.02,
            'primary_channels': ['GREENS BAYOU', 'HALLS BAYOU', 'GARNERS BAYOU']
        },
        'Q': {
            'name': 'Cedar Bayou',
            'short_name': 'Cedar',
            'effective_date': '2022-05-05',
            'size_gb': 0.02,
            'primary_channels': ['CEDAR BAYOU', 'LITTLE CEDAR BAYOU']
        },
        'R': {
            'name': 'Jackson Bayou',
            'short_name': 'Jackson',
            'effective_date': '2022-05-05',
            'size_gb': 0.01,
            'primary_channels': ['JACKSON BAYOU']
        },
        'S': {
            'name': 'Luce Bayou',
            'short_name': 'Luce',
            'effective_date': '2022-05-05',
            'size_gb': 0.01,
            'primary_channels': ['LUCE BAYOU']
        },
        'T': {
            'name': 'Barker',
            'short_name': 'Barker',
            'effective_date': '2022-05-05',
            'size_gb': 0.01,
            'primary_channels': ['BARKER DITCH']
        },
        'U': {
            'name': 'Addicks',
            'short_name': 'Addicks',
            'effective_date': '2022-05-05',
            'size_gb': 0.08,
            'primary_channels': []  # Reservoir, not a specific channel
        },
        'W': {
            'name': 'Buffalo Bayou',
            'short_name': 'Buffalo',
            'effective_date': '2022-05-05',
            'size_gb': 0.03,
            'primary_channels': ['BUFFALO BAYOU', 'UPPER BUFFALO BAYOU/CANE']
        }
    }

    # HMS Project catalog - embedded for standalone operation
    # Can also be loaded from CSV file
    HMS_PROJECTS = None  # Loaded on first access
    _catalog_df = None

    @classmethod
    def _load_catalog(cls) -> pd.DataFrame:
        """Load the HMS project catalog from embedded CSV data."""
        if cls._catalog_df is not None:
            return cls._catalog_df

        # Try to load from package data file first
        try:
            import importlib.resources as pkg_resources
            try:
                # Python 3.9+
                files = pkg_resources.files('hms_commander.data')
                csv_path = files.joinpath('m3_hms_catalog.csv')
                with pkg_resources.as_file(csv_path) as path:
                    cls._catalog_df = pd.read_csv(path)
                    logger.debug(f"Loaded catalog from package: {len(cls._catalog_df)} projects")
                    return cls._catalog_df
            except (TypeError, AttributeError):
                # Python 3.8
                with pkg_resources.open_text('hms_commander.data', 'm3_hms_catalog.csv') as f:
                    cls._catalog_df = pd.read_csv(f)
                    logger.debug(f"Loaded catalog from package: {len(cls._catalog_df)} projects")
                    return cls._catalog_df
        except Exception as e:
            logger.debug(f"Could not load from package resources: {e}")

        # Fallback: try relative path
        try:
            catalog_path = Path(__file__).parent / 'data' / 'm3_hms_catalog.csv'
            if catalog_path.exists():
                cls._catalog_df = pd.read_csv(catalog_path)
                logger.debug(f"Loaded catalog from file: {len(cls._catalog_df)} projects")
                return cls._catalog_df
        except Exception as e:
            logger.debug(f"Could not load from file: {e}")

        # Ultimate fallback: embedded data
        logger.warning("Using embedded catalog data (may be outdated)")
        cls._catalog_df = cls._get_embedded_catalog()
        return cls._catalog_df

    @classmethod
    def _get_embedded_catalog(cls) -> pd.DataFrame:
        """Return embedded catalog data as DataFrame."""
        # Minimal embedded catalog for fallback
        data = [
            {'model_id': 'A', 'model_name': 'Clear Creek', 'unit_id': 'A100-00-00',
             'hms_file': 'A1000000.hms', 'hms_version': '3.3', 'relative_path': 'A/HEC-HMS/A_A100-00-00'},
            {'model_id': 'D', 'model_name': 'Brays Bayou', 'unit_id': 'D100-00-00',
             'hms_file': 'D100_00_00.hms', 'hms_version': '3.3', 'relative_path': 'D/HEC-HMS/D_D100-00-00/D_D100-00-00'},
            {'model_id': 'W', 'model_name': 'Buffalo Bayou', 'unit_id': 'W100-00-00',
             'hms_file': 'W100_00_00.hms', 'hms_version': '3.3', 'relative_path': 'W/HEC-HMS/W100_00_00/W100_00_00'},
        ]
        return pd.DataFrame(data)

    @classmethod
    @log_call
    def list_models(cls, as_dataframe: bool = True) -> Union[pd.DataFrame, List[Dict]]:
        """
        List all available M3 Models that contain HMS projects.

        Args:
            as_dataframe: If True, returns DataFrame. If False, returns list of dicts.

        Returns:
            DataFrame or list with model information including:
            - model_id: Single letter identifier (A-W)
            - name: Watershed/bayou name
            - hms_project_count: Number of HMS projects in model
            - primary_channels: Main channels in watershed

        Example:
            >>> models = HmsM3Model.list_models()
            >>> print(models[['model_id', 'name', 'hms_project_count']])
        """
        catalog = cls._load_catalog()

        # Count projects per model
        project_counts = catalog.groupby('model_id').size().to_dict()

        models_list = []
        for model_id, info in cls.MODELS.items():
            count = project_counts.get(model_id, 0)
            if count > 0:  # Only include models with HMS projects
                model_dict = {
                    'model_id': model_id,
                    'name': info['name'],
                    'hms_project_count': count,
                    'effective_date': info['effective_date'],
                    'size_gb': info['size_gb'],
                    'primary_channels': ', '.join(info['primary_channels'])
                }
                models_list.append(model_dict)

        if as_dataframe:
            df = pd.DataFrame(models_list)
            logger.info(f"Listed {len(df)} M3 Models with HMS projects")
            return df
        else:
            logger.info(f"Listed {len(models_list)} M3 Models with HMS projects")
            return models_list

    @classmethod
    @log_call
    def list_projects(
        cls,
        model_id: Optional[str] = None,
        as_dataframe: bool = True
    ) -> Union[pd.DataFrame, List[Dict]]:
        """
        List available HMS projects from M3 Models.

        Args:
            model_id: Filter by model letter (e.g., 'D' for Brays Bayou).
                     If None, returns all projects.
            as_dataframe: If True, returns DataFrame. If False, returns list of dicts.

        Returns:
            DataFrame or list with HMS project information including:
            - model_id: Model letter
            - model_name: Watershed name
            - unit_id: HCFCD unit number
            - hms_file: HMS project filename
            - hms_version: HMS version (3.3 or 3.4)
            - design_storms: Available storm frequencies
            - loss_method: Hydrologic loss method
            - transform_method: Unit hydrograph method

        Example:
            >>> # All projects
            >>> all_projects = HmsM3Model.list_projects()
            >>>
            >>> # Projects for Brays Bayou only
            >>> brays = HmsM3Model.list_projects(model_id='D')
        """
        catalog = cls._load_catalog()

        if model_id is not None:
            model_id = model_id.upper()
            if model_id not in cls.MODELS:
                available = ', '.join(sorted(cls.MODELS.keys()))
                raise ValueError(f"Model '{model_id}' not found. Available: {available}")
            catalog = catalog[catalog['model_id'] == model_id]

        if as_dataframe:
            logger.info(f"Listed {len(catalog)} HMS projects")
            return catalog.copy()
        else:
            result = catalog.to_dict('records')
            logger.info(f"Listed {len(result)} HMS projects")
            return result

    @classmethod
    @log_call
    def get_model_info(cls, model_id: str) -> Dict:
        """
        Get detailed information about an M3 Model.

        Args:
            model_id: Single letter model identifier (e.g., 'D')

        Returns:
            Dictionary containing:
            - name: Watershed name
            - effective_date: FEMA effective date
            - size_gb: Download size
            - primary_channels: List of main channels
            - hms_projects: List of HMS project unit IDs
            - download_url: URL for model download

        Example:
            >>> info = HmsM3Model.get_model_info('D')
            >>> print(f"Model: {info['name']}")
            >>> print(f"HMS Projects: {info['hms_projects']}")
        """
        model_id = model_id.upper()

        if model_id not in cls.MODELS:
            available = ', '.join(sorted(cls.MODELS.keys()))
            raise ValueError(f"Model '{model_id}' not found. Available: {available}")

        info = cls.MODELS[model_id].copy()
        info['model_id'] = model_id
        info['download_url'] = cls._get_download_url(model_id)
        info['filename'] = cls._get_filename(model_id)

        # Add HMS project list
        catalog = cls._load_catalog()
        model_projects = catalog[catalog['model_id'] == model_id]
        info['hms_projects'] = model_projects['unit_id'].tolist()
        info['hms_project_count'] = len(model_projects)

        logger.info(f"Retrieved info for model '{model_id}': {info['name']}")
        return info

    @classmethod
    @log_call
    def get_project_info(cls, model_id: str, unit_id: str) -> Dict:
        """
        Get detailed information about a specific HMS project.

        Args:
            model_id: Model letter (e.g., 'D')
            unit_id: HCFCD unit number (e.g., 'D100-00-00')

        Returns:
            Dictionary with project metadata from catalog

        Example:
            >>> info = HmsM3Model.get_project_info('D', 'D100-00-00')
            >>> print(f"HMS Version: {info['hms_version']}")
            >>> print(f"Loss Method: {info['loss_method']}")
        """
        model_id = model_id.upper()
        catalog = cls._load_catalog()

        # Find matching project
        mask = (catalog['model_id'] == model_id) & (catalog['unit_id'] == unit_id)
        matches = catalog[mask]

        if len(matches) == 0:
            available = catalog[catalog['model_id'] == model_id]['unit_id'].tolist()
            raise ValueError(
                f"HMS project '{unit_id}' not found in model '{model_id}'. "
                f"Available: {', '.join(available)}"
            )

        info = matches.iloc[0].to_dict()
        logger.info(f"Retrieved info for project '{unit_id}'")
        return info

    @classmethod
    @log_call
    def get_project_by_channel(cls, channel_name: str) -> Optional[Tuple[str, str]]:
        """
        Find M3 Model and HMS project for a channel name.

        Args:
            channel_name: Name of channel (e.g., 'BRAYS BAYOU')

        Returns:
            Tuple of (model_id, unit_id) if found, None otherwise

        Example:
            >>> result = HmsM3Model.get_project_by_channel('BRAYS BAYOU')
            >>> if result:
            ...     model_id, unit_id = result
            ...     path = HmsM3Model.extract_project(model_id, unit_id)
        """
        channel_upper = channel_name.upper()

        for model_id, info in cls.MODELS.items():
            for primary_channel in info['primary_channels']:
                if channel_upper == primary_channel.upper():
                    # Found model, get first HMS project
                    catalog = cls._load_catalog()
                    model_projects = catalog[catalog['model_id'] == model_id]
                    if len(model_projects) > 0:
                        unit_id = model_projects.iloc[0]['unit_id']
                        logger.info(f"Channel '{channel_name}' -> Model {model_id}, Unit {unit_id}")
                        return (model_id, unit_id)

        logger.warning(f"No HMS project found for channel '{channel_name}'")
        return None

    @classmethod
    def _get_filename(cls, model_id: str) -> str:
        """Generate the zip filename for a model."""
        info = cls.MODELS[model_id.upper()]
        return f"{model_id.upper()}_{info['short_name']}_FEMA_Effective.zip"

    @classmethod
    def _get_download_url(cls, model_id: str) -> str:
        """Generate the full download URL for a model."""
        info = cls.MODELS[model_id.upper()]
        filename = cls._get_filename(model_id)
        effective_date = info['effective_date'] + ' 05:00'
        return f"{cls.base_url}{filename}?effectivedate={effective_date.replace(' ', '%20')}"

    @classmethod
    @log_call
    def extract_project(
        cls,
        model_id: str,
        unit_id: str,
        output_path: Optional[Union[str, Path]] = None,
        overwrite: bool = False,
        *,
        max_archive_bytes: int = 512 * 1024 * 1024,
        max_uncompressed_bytes: int = 2 * 1024 * 1024 * 1024,
        max_members: int = 10000,
        max_path_depth: int = 32,
    ) -> Path:
        """Download and stage one HMS project without deleting an existing copy.

        The complete nested archive is checked before publication. One common
        wrapper directory is removed, while nested project directories remain.
        Replacement retains an inventoried prior copy in a unique intake folder.

        Args:
            model_id: Model letter, for example 'D'.
            unit_id: Catalog unit identifier, for example 'D100-00-00'.
            output_path: Output root. Defaults to ./m3_hms_projects/.
            overwrite: Replace an existing project after staging and verification.
                False reuses the existing directory without downloading or checking it.
            max_archive_bytes: Byte ceiling for the download and nested ZIP;
                defaults to 512 MiB. Actual streamed bytes are checked.
            max_uncompressed_bytes: Total extracted project byte ceiling;
                defaults to 2 GiB. Increase deliberately for a reviewed delivery.
            max_members: Maximum entries in either ZIP; defaults to 10,000.
            max_path_depth: Maximum path components per ZIP entry, including
                a wrapper directory; defaults to 32.

        Returns:
            Path to the extracted project directory.

        Raises:
            ValueError: An identifier, archive path, entry or byte budget is invalid.
            FileNotFoundError: The selected nested HMS archive is absent.
            RuntimeError: Another intake holds the lock, or restoration needs review.
            OSError: Download storage, extraction or publication fails.
            requests.RequestException: Acquisition fails before publication.
            zipfile.BadZipFile: A ZIP directory or member CRC is invalid.

        Note:
            The output root must be writable only by trusted cooperating callers.
            Failed attempts and prior projects are retained for inspection. A process
            interruption between directory renames may require explicit recovery;
            never remove a stale intake lock while its writer is still active.
            This checks archive integrity, not engineering readiness. HMS 3.x
            execution still requires python2_compatible=True for Jython.
        """
        model_id = model_id.upper()
        cls.get_project_info(model_id, unit_id)
        base_output = cls.models_dir if output_path is None else Path(output_path)
        return _extract_m3_project(
            cls._get_download_url(model_id), Path(base_output), model_id, unit_id,
            overwrite=overwrite, max_archive_bytes=max_archive_bytes,
            max_uncompressed_bytes=max_uncompressed_bytes, max_members=max_members,
            max_path_depth=max_path_depth,
        )

    @classmethod
    @log_call
    def extract_model(
        cls,
        model_id: str,
        output_path: Optional[Union[str, Path]] = None,
        overwrite: bool = False
    ) -> Dict[str, Path]:
        """
        Extract all HMS projects from an M3 Model.

        Args:
            model_id: Model letter (e.g., 'G' for San Jacinto River)
            output_path: Base output directory
            overwrite: If True, overwrite existing extractions

        Returns:
            Dictionary mapping unit_id to extracted path

        Example:
            >>> # Extract all San Jacinto River HMS projects
            >>> paths = HmsM3Model.extract_model('G')
            >>> for unit_id, path in paths.items():
            ...     print(f"{unit_id}: {path}")
        """
        model_id = model_id.upper()

        if model_id not in cls.MODELS:
            available = ', '.join(sorted(cls.MODELS.keys()))
            raise ValueError(f"Model '{model_id}' not found. Available: {available}")

        catalog = cls._load_catalog()
        model_projects = catalog[catalog['model_id'] == model_id]

        if len(model_projects) == 0:
            logger.warning(f"No HMS projects found in model '{model_id}'")
            return {}

        extracted = {}
        for _, row in model_projects.iterrows():
            unit_id = row['unit_id']
            try:
                path = cls.extract_project(
                    model_id,
                    unit_id,
                    output_path=output_path,
                    overwrite=overwrite
                )
                extracted[unit_id] = path
            except Exception as e:
                logger.error(f"Failed to extract '{unit_id}': {e}")

        logger.info(f"Extracted {len(extracted)} of {len(model_projects)} HMS projects from model '{model_id}'")
        return extracted

    @classmethod
    @log_call
    def is_project_extracted(
        cls,
        model_id: str,
        unit_id: str,
        output_path: Optional[Union[str, Path]] = None
    ) -> bool:
        """
        Check if an HMS project has already been extracted.

        Args:
            model_id: Model letter
            unit_id: HCFCD unit number
            output_path: Base output directory (default: ./m3_hms_projects/)

        Returns:
            True if project folder exists

        Example:
            >>> if not HmsM3Model.is_project_extracted('D', 'D100-00-00'):
            ...     HmsM3Model.extract_project('D', 'D100-00-00')
        """
        model_id = model_id.upper()

        if output_path is None:
            base_output = cls.models_dir
        else:
            base_output = Path(output_path)
            if not base_output.is_absolute():
                base_output = Path.cwd() / base_output

        project_path = base_output / model_id / unit_id
        exists = project_path.exists() and project_path.is_dir()

        logger.debug(f"Project '{model_id}/{unit_id}' extracted: {exists}")
        return exists

    @classmethod
    @log_call
    def clean_projects_directory(
        cls,
        output_path: Optional[Union[str, Path]] = None
    ) -> None:
        """
        Remove all extracted M3 HMS projects.

        Args:
            output_path: Directory to clean (default: ./m3_hms_projects/)

        Example:
            >>> HmsM3Model.clean_projects_directory()
        """
        if output_path is None:
            target = cls.models_dir
        else:
            target = Path(output_path)
            if not target.is_absolute():
                target = Path.cwd() / target

        if target.exists():
            logger.info(f"Removing all projects from: {target}")
            shutil.rmtree(target)
            logger.info("Projects directory cleaned")
        else:
            logger.info(f"Directory does not exist: {target}")

        target.mkdir(parents=True, exist_ok=True)

    @classmethod
    def get_statistics(cls) -> Dict:
        """
        Get summary statistics about M3 HMS projects.

        Returns:
            Dictionary with catalog statistics

        Example:
            >>> stats = HmsM3Model.get_statistics()
            >>> print(f"Total projects: {stats['total_projects']}")
        """
        catalog = cls._load_catalog()

        stats = {
            'total_projects': len(catalog),
            'total_models': catalog['model_id'].nunique(),
            'models_with_most_projects': catalog['model_id'].value_counts().head(3).to_dict(),
            'hms_versions': catalog['hms_version'].value_counts().to_dict() if 'hms_version' in catalog.columns else {},
            'loss_methods': catalog['loss_method'].value_counts().to_dict() if 'loss_method' in catalog.columns else {},
        }

        return stats
