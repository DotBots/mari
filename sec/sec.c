/**
 * @file
 * @ingroup sec
 *
 * @brief  Generic definition of the "sec" module.
 *
 * @author Geovane Fedrecheski <geovane.fedrecheski@inria.fr>
 *
 * @copyright Inria, 2025-now
 */

#if defined(NRF52840_XXAA)
#include "sec_default.c"
#elif defined(NRF5340_XXAA) && defined(NRF_NETWORK)
#include "sec_mock.c"
#endif
