/*
 * SPDX-FileCopyrightText: 2022-2025 Espressif Systems (Shanghai) CO LTD
 *
 * SPDX-License-Identifier: Unlicense OR CC0-1.0
 */

/*
 * USB CDC echo test, based on the tusb_serial_device example.
 *
 * The host (tools/usb_in_test.py) sends n bytes; the device queues every byte it receives back on the CDC port, as
 * received. The host checks that every echo arrives complete and byte-exact.
 *
 * The echo runs in the RX callback, i.e. in the TinyUSB task: that task is the only CDC writer.
 */

#include <stdint.h>
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "tinyusb.h"
#include "tinyusb_default_config.h"
#include "tinyusb_cdc_acm.h"
#include "sdkconfig.h"

static const char *TAG = "example";
static uint8_t rx_buf[CONFIG_TINYUSB_CDC_RX_BUFSIZE + 1];
static uint32_t echo_bytes;     // Bytes queued for echo since the last line state change (TinyUSB task only)

/**
 * @brief CDC device RX callback: queue the received bytes back on the CDC port
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

    /* write back */
    if (tud_cdc_n_write_available(itf) < rx_size) {
        ESP_LOGW(TAG, "TX FIFO busy (%u bytes free), %u echo bytes dropped",
                 (unsigned)tud_cdc_n_write_available(itf), (unsigned)rx_size);
        return;
    }
    size_t queued = tinyusb_cdcacm_write_queue(itf, rx_buf, rx_size);  // copies the bytes into the TX FIFO
    if (queued != rx_size) {
        ESP_LOGE(TAG, "echo queued %u/%u bytes", (unsigned)queued, (unsigned)rx_size);
    }
    echo_bytes += queued;
    /* Non-blocking flush (we run in the TinyUSB task): ESP_ERR_NOT_FINISHED only means the transfer goes on */
    esp_err_t err = tinyusb_cdcacm_write_flush(itf, 0);
    if (err != ESP_OK && err != ESP_ERR_NOT_FINISHED) {
        ESP_LOGE(TAG, "CDC ACM write flush error: %s", esp_err_to_name(err));
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
    ESP_LOGI(TAG, "Line state changed on channel %d: DTR:%d, RTS:%d, echoed %u bytes", itf, dtr, rts,
             (unsigned)echo_bytes);
    echo_bytes = 0;
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

    ESP_LOGI(TAG, "USB initialization DONE, echo test ready");
}
