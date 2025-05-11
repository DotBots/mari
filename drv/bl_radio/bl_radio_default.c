/**
 * @file
 * @ingroup bsp_radio
 *
 * @brief  nRF52833-specific definition of the "radio" bsp module.
 *
 * @author Said Alvarado-Marin <said-alexander.alvarado-marin@inria.fr>
 * @author Diego Badillo-San-Juan <diego.badillo-san-juan@inria.fr>
 *
 * @copyright Inria, 2022-2024
 */
#include <nrf.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <assert.h>

#include "bl_clock.h"
#include "bl_timer_hf.h"
#include "bl_radio.h"

//=========================== defines ==========================================

#if defined(NRF5340_XXAA) && defined(NRF_NETWORK)
#define NRF_RADIO NRF_RADIO_NS
#endif

#if defined(NRF5340_XXAA) && defined(NRF_NETWORK)
#define RADIO_INTERRUPT_PRIORITY 2
#else
#define RADIO_INTERRUPT_PRIORITY 1
#endif

#define RADIO_TIFS 0U ///< Inter frame spacing in us. zero means IFS is enforced by software, not the hardware

#define RADIO_SHORTS_COMMON (RADIO_SHORTS_END_DISABLE_Enabled << RADIO_SHORTS_END_DISABLE_Pos) |                 \
                                (RADIO_SHORTS_ADDRESS_RSSISTART_Enabled << RADIO_SHORTS_ADDRESS_RSSISTART_Pos) | \
                                (RADIO_SHORTS_DISABLED_RSSISTOP_Enabled << RADIO_SHORTS_DISABLED_RSSISTOP_Pos)

#define RADIO_INTERRUPTS (RADIO_INTENSET_ADDRESS_Enabled << RADIO_INTENSET_ADDRESS_Pos) | \
                             (RADIO_INTENSET_END_Enabled << RADIO_INTENSET_END_Pos) |     \
                             (RADIO_INTENSET_DISABLED_Enabled << RADIO_INTENSET_DISABLED_Pos)

#define RADIO_STATE_IDLE 0x00
#define RADIO_STATE_RX 0x01
#define RADIO_STATE_TX 0x02
#define RADIO_STATE_BUSY 0x04

typedef struct __attribute__((packed))
{
    uint8_t header;                             ///< PDU header (depends on the type of PDU - advertising physical channel or Data physical channel)
    uint8_t length;                             ///< Length of the payload + MIC (if any)
    uint8_t payload[BL_BLE_PAYLOAD_MAX_LENGTH]; ///< Payload + MIC (if any) (BL_BLE_PAYLOAD_MAX_LENGTH > BL_IEEE802154_PAYLOAD_MAX_LENGTH)
} radio_pdu_t;

typedef struct
{
    radio_pdu_t pdu;                ///< Variable that stores the radio PDU (protocol data unit) that arrives and the radio packets that are about to be sent.
    bool pending_rx_read;           ///< Flag to indicate that a PDU has been received, but not yet read by the application.
    radio_ts_packet_t start_pac_cb; ///< Function pointer, stores the callback to capture the start of the packet.
    radio_ts_packet_t end_pac_cb;   ///< Function pointer, stores the callback to capture the end of the packet.
    uint8_t state;                  ///< Internal state of the radio
    bl_radio_mode_t mode;           ///< PHY protocol used by the radio (BLE, IEEE 802.15.4)
} radio_vars_t;

//=========================== variables ========================================

static const uint8_t _ble_chan_to_freq[40] = {
    4, 6, 8,
    10, 12, 14, 16, 18,
    20, 22, 24, 28,
    30, 32, 34, 36, 38,
    40, 42, 44, 46, 48,
    50, 52, 54, 56, 58,
    60, 62, 64, 66, 68,
    70, 72, 74, 76, 78,
    2, 26, 80 // Advertising channels
};

static radio_vars_t radio_vars = {0};

//========================== prototypes ========================================

static void _radio_enable(void);

//=========================== public ===========================================

void bl_radio_init(radio_ts_packet_t start_pac_cb, radio_ts_packet_t end_pac_cb, bl_radio_mode_t mode)
{

#if defined(NRF5340_XXAA)
    // On nrf53 configure constant latency mode for better performances
    NRF_POWER_NS->TASKS_CONSTLAT = 1;
#endif

    // Reset radio to its initial values
    NRF_RADIO->POWER = (RADIO_POWER_POWER_Disabled << RADIO_POWER_POWER_Pos);
    NRF_RADIO->POWER = (RADIO_POWER_POWER_Enabled << RADIO_POWER_POWER_Pos);

#if defined(NRF5340_XXAA)
    // Copy all the RADIO trim values from FICR into the target addresses (from errata v1.6 - 3.29 [158])
    for (uint32_t index = 0; index < 32ul && NRF_FICR_NS->TRIMCNF[index].ADDR != (uint32_t *)0xFFFFFFFFul; index++)
    {
        if (((uint32_t)NRF_FICR_NS->TRIMCNF[index].ADDR & 0xFFFFF000ul) == (volatile uint32_t)NRF_RADIO_NS)
        {
            *((volatile uint32_t *)NRF_FICR_NS->TRIMCNF[index].ADDR) = NRF_FICR_NS->TRIMCNF[index].DATA;
        }
    }
#endif

    // General configuration of the radio.
    radio_vars.mode = mode; // Set global radio mode
    if (mode == BL_RADIO_IEEE802154_250Kbit)
    {
        // Configure IEEE 802.15.4 mode
        NRF_RADIO->MODE = (RADIO_MODE_MODE_Ieee802154_250Kbit << RADIO_MODE_MODE_Pos);
    }
    else
    {
        // Configure BLE modes (e.g., BLE 1Mbit, 2Mbit, etc.)
        NRF_RADIO->MODE = ((RADIO_MODE_MODE_Ble_1Mbit + mode) << RADIO_MODE_MODE_Pos);
    }

#if defined(NRF5340_XXAA)
    // From errata v1.6 - 3.15 [117] RADIO: Changing MODE requires additional configuration
    if (mode == BL_RADIO_BLE_2MBit)
    {
        *((volatile uint32_t *)0x41008588) = *((volatile uint32_t *)0x01FF0084);
    }
    else
    {
        *((volatile uint32_t *)0x41008588) = *((volatile uint32_t *)0x01FF0080);
    }
#endif
    // Packet configuration of the radio
    if (mode == BL_RADIO_IEEE802154_250Kbit)
    {
        NRF_RADIO->TXPOWER = (RADIO_TXPOWER_TXPOWER_0dBm << RADIO_TXPOWER_TXPOWER_Pos); // Set transmission power to 0dBm

        // Packet configuration register 0
        NRF_RADIO->PCNF0 = (0 << RADIO_PCNF0_S1LEN_Pos) |                         // S1 field length in bits
                           (0 << RADIO_PCNF0_S0LEN_Pos) |                         // S0 field length in bytes
                           (8 << RADIO_PCNF0_LFLEN_Pos) |                         // 8-bit length field
                           (RADIO_PCNF0_PLEN_32bitZero << RADIO_PCNF0_PLEN_Pos) | // 4 bytes that are all zero for IEEE 802.15.4
                           (RADIO_PCNF0_CRCINC_Exclude << RADIO_PCNF0_CRCINC_Pos);

        // // Packet configuration register 1
        NRF_RADIO->PCNF1 = (BL_IEEE802154_PAYLOAD_MAX_LENGTH << RADIO_PCNF1_MAXLEN_Pos) | // Max payload of 127 bytes
                           (0 << RADIO_PCNF1_STATLEN_Pos) |                               // 0 bytes added to payload length
                           (RADIO_PCNF1_ENDIAN_Little << RADIO_PCNF1_ENDIAN_Pos) |        // Little-endian format
                           (0 << RADIO_PCNF1_BALEN_Pos) |                                 // Base address length
                           (RADIO_PCNF1_WHITEEN_Enabled << RADIO_PCNF1_WHITEEN_Pos);      // Enable whitening
    }
    else if (mode == BL_RADIO_BLE_1MBit || mode == BL_RADIO_BLE_2MBit)
    {
        NRF_RADIO->TXPOWER = (RADIO_TXPOWER_TXPOWER_0dBm << RADIO_TXPOWER_TXPOWER_Pos); // 0dBm == 1mW Power output
        NRF_RADIO->PCNF0 = (0 << RADIO_PCNF0_S1LEN_Pos) |                               // S1 field length in bits
                           (1 << RADIO_PCNF0_S0LEN_Pos) |                               // S0 field length in bytes
                           (8 << RADIO_PCNF0_LFLEN_Pos) |                               // LENGTH field length in bits
                           (RADIO_PCNF0_PLEN_8bit << RADIO_PCNF0_PLEN_Pos);             // PREAMBLE length is 1 byte in BLE 1Mbit/s and 2Mbit/s

        NRF_RADIO->PCNF1 = (4UL << RADIO_PCNF1_BALEN_Pos) | // The base address is 4 Bytes long
                           (BL_BLE_PAYLOAD_MAX_LENGTH << RADIO_PCNF1_MAXLEN_Pos) |
                           (0 << RADIO_PCNF1_STATLEN_Pos) |
                           (RADIO_PCNF1_ENDIAN_Little << RADIO_PCNF1_ENDIAN_Pos) |   // Make the on air packet be little endian (this enables some useful features)
                           (RADIO_PCNF1_WHITEEN_Enabled << RADIO_PCNF1_WHITEEN_Pos); // Enable data whitening feature.
    }
    else
    { // Long ranges modes (125KBit/500KBit)
#if defined(NRF5340_XXAA)
        NRF_RADIO->TXPOWER = (RADIO_TXPOWER_TXPOWER_0dBm << RADIO_TXPOWER_TXPOWER_Pos); // 0dBm Power output
#else
        NRF_RADIO->TXPOWER = (RADIO_TXPOWER_TXPOWER_Pos8dBm << RADIO_TXPOWER_TXPOWER_Pos); // 8dBm Power output
#endif

        // Coded PHY (Long Range)
        NRF_RADIO->PCNF0 = (0 << RADIO_PCNF0_S1LEN_Pos) |
                           (1 << RADIO_PCNF0_S0LEN_Pos) |
                           (8 << RADIO_PCNF0_LFLEN_Pos) |
                           (3 << RADIO_PCNF0_TERMLEN_Pos) |
                           (2 << RADIO_PCNF0_CILEN_Pos) |
                           (RADIO_PCNF0_PLEN_LongRange << RADIO_PCNF0_PLEN_Pos);

        NRF_RADIO->PCNF1 = (RADIO_PCNF1_WHITEEN_Disabled << RADIO_PCNF1_WHITEEN_Pos) |
                           (RADIO_PCNF1_ENDIAN_Little << RADIO_PCNF1_ENDIAN_Pos) |
                           (3 << RADIO_PCNF1_BALEN_Pos) |
                           (0 << RADIO_PCNF1_STATLEN_Pos) |
                           (BL_BLE_PAYLOAD_MAX_LENGTH << RADIO_PCNF1_MAXLEN_Pos);
    }

    // Broadcast Address configuration
    bl_radio_set_network_base_address(0, DEFAULT_BROADCAST_ADDRESS_TOP);
    bl_radio_set_network_prefix_address(0,  DEFAULT_BROADCAST_ADDRESS_BOT);
    NRF_RADIO->TXADDRESS = 7;                                                                  // Always send using logical address 7
    NRF_RADIO->RXADDRESSES = (RADIO_RXADDRESSES_ADDR0_Enabled << RADIO_RXADDRESSES_ADDR0_Pos); // Receive from the broadcast (logical address 0)

    // Inter frame spacing in us
    NRF_RADIO->TIFS = RADIO_TIFS;

    // Enable Fast TX Ramp Up
    NRF_RADIO->MODECNF0 = (RADIO_MODECNF0_RU_Fast << RADIO_MODECNF0_RU_Pos) |
                          (RADIO_MODECNF0_DTX_Center << RADIO_MODECNF0_DTX_Pos);

    // CRC Config
    if (mode == BL_RADIO_IEEE802154_250Kbit)
    {
        NRF_RADIO->CRCCNF = (RADIO_CRCCNF_LEN_Two << RADIO_CRCCNF_LEN_Pos) |                 // 16-bit (2 bytes) CRC
                            (RADIO_CRCCNF_SKIPADDR_Ieee802154 << RADIO_CRCCNF_SKIPADDR_Pos); // CRCCNF = 0x202 for IEEE 802.15.4
        NRF_RADIO->CRCINIT = 0;                                                              // The start value used by IEEE 802.15.4 is zero
        NRF_RADIO->CRCPOLY = 0x11021;
    }
    else
    {
        NRF_RADIO->CRCCNF = (RADIO_CRCCNF_LEN_Three << RADIO_CRCCNF_LEN_Pos) | (RADIO_CRCCNF_SKIPADDR_Skip << RADIO_CRCCNF_SKIPADDR_Pos); // Checksum uses 3 bytes, and is enabled.
        NRF_RADIO->CRCINIT = 0xFFFFUL;                                                                                                    // initial value
        NRF_RADIO->CRCPOLY = 0x00065b;                                                                                                    // CRC poly: x^16 + x^12^x^5 + 1
    }

    // Configure pointer to PDU for EasyDMA
    if (mode == BL_RADIO_IEEE802154_250Kbit)
    {
        NRF_RADIO->PACKETPTR = (uint32_t)((uint8_t *)&radio_vars.pdu + 1); // Skip header for IEEE 802.15.4
    }
    else
    {
        NRF_RADIO->PACKETPTR = (uint32_t)&radio_vars.pdu;
    }

    // Assign the callbacks that will be called in the RADIO_IRQHandler
    radio_vars.start_pac_cb = start_pac_cb;
    radio_vars.end_pac_cb = end_pac_cb;
    radio_vars.state = RADIO_STATE_IDLE;

    // Configure the external High-frequency Clock. (Needed for correct operation)
    bl_hfclk_init();

    // Configure the Interruptions
    NVIC_SetPriority(RADIO_IRQn, RADIO_INTERRUPT_PRIORITY); // Set priority for Radio interrupts to 1
    // Clear all radio interruptions
    NRF_RADIO->INTENCLR = 0xffffffff;
    NVIC_EnableIRQ(RADIO_IRQn);
}

void bl_radio_set_frequency(uint8_t freq)
{
    NRF_RADIO->FREQUENCY = freq << RADIO_FREQUENCY_FREQUENCY_Pos;
}

void bl_radio_set_channel(uint8_t channel)
{
    uint8_t freq;
    if (radio_vars.mode == BL_RADIO_IEEE802154_250Kbit)
    {
        assert(channel >= 11 && channel <= 26 && "Channel value must be between 11 and 26 for IEEE 802.15.4");
        freq = 5 * (channel - 10); // Frequency offset in MHz from 2400 MHz
    }
    else
    {
        freq = _ble_chan_to_freq[channel];
    }

    bl_radio_set_frequency(freq);
}

void bl_radio_disable(void)
{
    NRF_RADIO->INTENCLR = RADIO_INTERRUPTS;
    NRF_RADIO->SHORTS = 0;
    NRF_RADIO->EVENTS_DISABLED = 0;
    NRF_RADIO->TASKS_DISABLE = RADIO_TASKS_DISABLE_TASKS_DISABLE_Trigger << RADIO_TASKS_DISABLE_TASKS_DISABLE_Pos;
    while (NRF_RADIO->EVENTS_DISABLED == 0)
    {
    }
    radio_vars.state = RADIO_STATE_IDLE;
}

int8_t bl_radio_rssi(void)
{
    return (uint8_t)NRF_RADIO->RSSISAMPLE * -1;
}

bool bl_radio_pending_rx_read(void)
{
    return radio_vars.pending_rx_read;
}

void bl_radio_get_rx_packet(uint8_t *packet, uint8_t *length)
{
    *length = radio_vars.pdu.length;
    memcpy(packet, radio_vars.pdu.payload, radio_vars.pdu.length);
    radio_vars.pending_rx_read = false;
}

//--------------------------- send and receive --------------------------------

// TODO: split into bl_radio_rx_prepare and bl_radio_rx_dispatch
//       includes disabling the RXREADY_START short
void bl_radio_rx(void)
{
    if (radio_vars.state != RADIO_STATE_IDLE)
    {
        return;
    }

    // enable the radio shorts and interrupts
    NRF_RADIO->SHORTS = RADIO_SHORTS_COMMON | (RADIO_SHORTS_RXREADY_START_Enabled << RADIO_SHORTS_RXREADY_START_Pos);
    _radio_enable();

    NRF_RADIO->TASKS_RXEN = RADIO_TASKS_RXEN_TASKS_RXEN_Trigger;
    radio_vars.state = RADIO_STATE_RX;
}

void bl_radio_tx_prepare(const uint8_t *tx_buffer, uint8_t length)
{
    // TODO: check for IDLE?
    radio_vars.pdu.length = length;
    memcpy(radio_vars.pdu.payload, tx_buffer, length);

    // ramp up the radio for tx (packet will not be sent yet)
    NRF_RADIO->TASKS_TXEN = RADIO_TASKS_TXEN_TASKS_TXEN_Trigger << RADIO_TASKS_TXEN_TASKS_TXEN_Pos;
}

void bl_radio_tx_dispatch(void)
{
    if (radio_vars.state != RADIO_STATE_IDLE)
    {
        return;
    }

    // enable the radio shorts and interrupts
    NRF_RADIO->SHORTS = RADIO_SHORTS_COMMON;
    _radio_enable();

    // tell radio to start transmission
    NRF_RADIO->TASKS_START = RADIO_TASKS_START_TASKS_START_Trigger << RADIO_TASKS_START_TASKS_START_Pos;
    radio_vars.state = RADIO_STATE_TX;
}

void bl_radio_set_network_base_address(uint8_t base,  uint32_t addr){

    if (base == 0){
        NRF_RADIO->BASE0 = addr;
    }
    if (base==1){
        NRF_RADIO->BASE1 = addr;
    }
}

void bl_radio_set_network_prefix_address(uint8_t prefix,  uint8_t addr){


    switch (prefix)
    {
    case 0:
        NRF_RADIO->PREFIX0 &= ~RADIO_PREFIX0_AP0_Msk; // Erase that particular prefix
        NRF_RADIO->PREFIX0 |= addr << RADIO_PREFIX0_AP0_Pos; // write the new prefix
        break;
    case 1:
        NRF_RADIO->PREFIX0 &= ~RADIO_PREFIX0_AP1_Msk;
        NRF_RADIO->PREFIX0 |= addr << RADIO_PREFIX0_AP1_Pos;
        break;
    case 2:
        NRF_RADIO->PREFIX0 &= ~RADIO_PREFIX0_AP2_Msk;
        NRF_RADIO->PREFIX0 |= addr << RADIO_PREFIX0_AP2_Pos;
        break;
    case 3:
        NRF_RADIO->PREFIX0 &= ~RADIO_PREFIX0_AP3_Msk;
        NRF_RADIO->PREFIX0 |= addr << RADIO_PREFIX0_AP3_Pos;
        break;
    case 4:
        NRF_RADIO->PREFIX1 &= ~RADIO_PREFIX1_AP4_Msk;
        NRF_RADIO->PREFIX1 |= addr << RADIO_PREFIX1_AP4_Pos;
        break;
    case 5:
        NRF_RADIO->PREFIX1 &= ~RADIO_PREFIX1_AP5_Msk;
        NRF_RADIO->PREFIX1 |= addr << RADIO_PREFIX1_AP5_Pos;
        break;
    case 6:
        NRF_RADIO->PREFIX1 &= ~RADIO_PREFIX1_AP6_Msk;
        NRF_RADIO->PREFIX1 |= addr << RADIO_PREFIX1_AP6_Pos;
        break;
    case 7:
        NRF_RADIO->PREFIX1 &= ~RADIO_PREFIX1_AP7_Msk;
        NRF_RADIO->PREFIX1 |= addr << RADIO_PREFIX1_AP7_Pos;
        break;
    
    default:
        break;
    }
}

//=========================== private ==========================================

static void _radio_enable(void)
{
    NRF_RADIO->EVENTS_ADDRESS = 0;
    NRF_RADIO->EVENTS_END = 0;
    NRF_RADIO->EVENTS_DISABLED = 0;
    NRF_RADIO->INTENSET = RADIO_INTERRUPTS;
}

//=========================== interrupt handlers ===============================

/**
 * @brief Interruption handler for the Radio.
 *
 * This function will be called each time a radio packet is received.
 * it will clear the interrupt, copy the last received packet
 * and called the user-defined callbacks to process the packet.
 *
 */
void RADIO_IRQHandler(void)
{
    uint8_t timer_dev = 2; // FIXME: pass by parameter, or have radio report it somehow
    uint32_t now_ts = bl_timer_hf_now(timer_dev);
    uint8_t dbg = 0;

    // just started sending or receiving: clear interrupt flag, set radio as busy, and report packet start time
    if (NRF_RADIO->EVENTS_ADDRESS)
    {
        NRF_RADIO->EVENTS_ADDRESS = 0;
        dbg |= 1;
        radio_vars.state |= RADIO_STATE_BUSY;
        if (radio_vars.start_pac_cb)
        {
            radio_vars.start_pac_cb(now_ts);
        }
    }

    // just finished sending or receiving: clear interrupt flag and report packet end time
    if (NRF_RADIO->EVENTS_END)
    {
        NRF_RADIO->EVENTS_END = 0;
        dbg |= 2;
        if (radio_vars.state == (RADIO_STATE_BUSY | RADIO_STATE_RX))
        {
            // if rx, check the CRC
            if (NRF_RADIO->CRCSTATUS != RADIO_CRCSTATUS_CRCSTATUS_CRCOk)
            {
                puts("Invalid CRC");
            }
            else
            {
                if (radio_vars.end_pac_cb)
                {
                    radio_vars.pending_rx_read = true;
                    radio_vars.end_pac_cb(now_ts);
                }
            }
        }
        else if (radio_vars.state == (RADIO_STATE_BUSY | RADIO_STATE_TX))
        {
            if (radio_vars.end_pac_cb)
            {
                radio_vars.end_pac_cb(now_ts);
            }
        }
    }

    // radio has been disabled: clear interrupt flag, disable interrupts, and stay idle (off)
    if (NRF_RADIO->EVENTS_DISABLED)
    {
        NRF_RADIO->EVENTS_DISABLED = 0;
        dbg |= 4;

        // disable interrupts and shorts
        NRF_RADIO->INTENCLR = RADIO_INTERRUPTS;
        NRF_RADIO->SHORTS = 0;

        // udpate state
        radio_vars.state = RADIO_STATE_IDLE;
    }
    if (dbg)
        radio_vars.state = radio_vars.state;
}
