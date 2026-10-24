/*
 * SPDX-FileCopyrightText: 2022-2025 Espressif Systems (Shanghai) CO LTD
 *
 * SPDX-License-Identifier: Unlicense OR CC0-1.0
 */

/*
 * USB CDC IN (device to host) test, based on the tusb_serial_device example.
 *
 * The host (tools/usb_in_test.py) sends an 8-byte request {magic, size}. The device allocates a buffer of `size`
 * bytes, fills it with fixed test data (in_test_head, then IN_TEST_FILL), queues the whole buffer on the CDC port and
 * frees it. The host checks that every reply arrives complete and byte-exact.
 *
 * The request is handled in the RX callback, i.e. in the TinyUSB task: that task is the only CDC writer.
 */

#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include "esp_log.h"
#include "esp_rom_crc.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "tinyusb.h"
#include "tinyusb_default_config.h"
#include "tinyusb_cdc_acm.h"
#include "sdkconfig.h"

static const char *TAG = "example";
static uint8_t rx_buf[CONFIG_TINYUSB_CDC_RX_BUFSIZE + 1];

/**
 * @brief IN test request sent by the host, little-endian
 */
#define IN_TEST_MAGIC    0x4E495354u                    // "TSIN"
#define IN_TEST_MAX_SIZE CONFIG_TINYUSB_CDC_TX_BUFSIZE  // a reply is queued at once, so it must fit the TX FIFO
#define IN_TEST_FILL     0xAA                           // reply bytes after in_test_head

typedef struct __attribute__((packed)) {
    uint32_t magic;     // IN_TEST_MAGIC
    uint32_t size;      // Reply length in bytes, 1..IN_TEST_MAX_SIZE
} in_test_request_t;

/**
 * @brief Fixed test data: the reply starts with these 16 bytes (an ID, as in a "read ID" reply of our product),
 *        the rest of the reply is IN_TEST_FILL
 */
static const uint8_t in_test_head[16] = {
    0x04, 0xC8, 0xF4, 0xCF, 0x7D, 0x5E, 0xB0, 0x4F, 0xB6, 0xFF, 0xC5, 0xD6, 0x7D, 0x96, 0x63, 0x57,
};

static uint8_t req_buf[sizeof(in_test_request_t)];  // Request bytes received so far (TinyUSB task only)
static size_t req_len;
static uint32_t reply_count;                        // Replies queued, numbers the log line

/**
 * @brief Build the reply of one request in a malloc'd buffer, queue it on the CDC port and free the buffer
 *
 * @param[in] itf CDC device index
 * @param[in] req Request
 */
static void in_test_reply(int itf, const in_test_request_t *req)
{
    if (req->magic != IN_TEST_MAGIC || req->size == 0 || req->size > IN_TEST_MAX_SIZE) {
        ESP_LOGW(TAG, "bad request: magic 0x%08X size %u", (unsigned)req->magic, (unsigned)req->size);
        return;
    }
    uint8_t *reply = malloc(req->size);
    if (reply == NULL) {
        ESP_LOGE(TAG, "no memory for a %u-byte reply", (unsigned)req->size);
        return;
    }
    memset(reply, IN_TEST_FILL, req->size);
    memcpy(reply, in_test_head, req->size < sizeof(in_test_head) ? req->size : sizeof(in_test_head));

    ESP_LOGI(TAG, "reply #%u size %u crc32 %08X", (unsigned)reply_count++, (unsigned)req->size,
             (unsigned)esp_rom_crc32_le(0, reply, req->size));
    if (tud_cdc_n_write_available(itf) < req->size) {
        ESP_LOGW(TAG, "TX FIFO busy (%u bytes free), reply dropped", (unsigned)tud_cdc_n_write_available(itf));
    } else {
        size_t queued = tinyusb_cdcacm_write_queue(itf, reply, req->size);  // copies the reply into the TX FIFO
        if (queued != req->size) {
            ESP_LOGE(TAG, "reply queued %u/%u bytes", (unsigned)queued, (unsigned)req->size);
        }
        /* Non-blocking flush (we run in the TinyUSB task): ESP_ERR_NOT_FINISHED only means the transfer goes on */
        esp_err_t err = tinyusb_cdcacm_write_flush(itf, 0);
        if (err != ESP_OK && err != ESP_ERR_NOT_FINISHED) {
            ESP_LOGE(TAG, "CDC ACM write flush error: %s", esp_err_to_name(err));
        }
    }
    free(reply);
}

/**
 * @brief CDC device RX callback
 *
 * CDC device signals, that new data were received
 *
 * @param[in] itf   CDC device index
 * @param[in] event CDC event type
 */
void tinyusb_cdc_rx_callback(int itf, cdcacm_event_t *event)
{
    /* initialization */
    size_t rx_size = 0;

    /* read */
    esp_err_t ret = tinyusb_cdcacm_read(itf, rx_buf, CONFIG_TINYUSB_CDC_RX_BUFSIZE, &rx_size);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Read Error");
        return;
    }

    /* requests may arrive split or back to back */
    for (size_t i = 0; i < rx_size; i++) {
        req_buf[req_len++] = rx_buf[i];
        if (req_len == sizeof(req_buf)) {
            in_test_request_t req;
            memcpy(&req, req_buf, sizeof(req));
            req_len = 0;
            in_test_reply(itf, &req);
        }
    }
}

/**
 * @brief CDC device line change callback
 *
 * CDC device signals, that the DTR, RTS states changed
 *
 * @param[in] itf   CDC device index
 * @param[in] event CDC event type
 */
void tinyusb_cdc_line_state_changed_callback(int itf, cdcacm_event_t *event)
{
    int dtr = event->line_state_changed_data.dtr;
    int rts = event->line_state_changed_data.rts;
    ESP_LOGI(TAG, "Line state changed on channel %d: DTR:%d, RTS:%d", itf, dtr, rts);
    req_len = 0;    // a new host session starts with a whole request
}

void app_main(void)
{
    ESP_LOGI(TAG, "USB initialization");
    const tinyusb_config_t tusb_cfg = TINYUSB_DEFAULT_CONFIG();
    ESP_ERROR_CHECK(tinyusb_driver_install(&tusb_cfg));

    tinyusb_config_cdcacm_t acm_cfg = {
        .cdc_port = TINYUSB_CDC_ACM_0,
        .callback_rx = &tinyusb_cdc_rx_callback, // the first way to register a callback
        .callback_rx_wanted_char = NULL,
        .callback_line_state_changed = NULL,
        .callback_line_coding_changed = NULL
    };

    ESP_ERROR_CHECK(tinyusb_cdcacm_init(&acm_cfg));
    /* the second way to register a callback */
    ESP_ERROR_CHECK(tinyusb_cdcacm_register_callback(
                        TINYUSB_CDC_ACM_0,
                        CDC_EVENT_LINE_STATE_CHANGED,
                        &tinyusb_cdc_line_state_changed_callback));

    ESP_LOGI(TAG, "USB initialization DONE, IN test ready (max reply %u bytes)", (unsigned)IN_TEST_MAX_SIZE);
}
