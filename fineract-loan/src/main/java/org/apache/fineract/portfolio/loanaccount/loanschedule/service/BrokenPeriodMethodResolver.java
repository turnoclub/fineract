/**
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements. See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership. The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License. You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing,
 * software distributed under the License is distributed on an
 * "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
 * KIND, either express or implied. See the License for the
 * specific language governing permissions and limitations
 * under the License.
 */
package org.apache.fineract.portfolio.loanaccount.loanschedule.service;

import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.dao.DataAccessException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;

/**
 * Resolves the per-product broken-period method for the FIRST installment from the
 * {@code loan_product_first_emi_config} datatable ({@code broken_period_method} column), so each loan product can use a
 * different lender-specific first-installment convention without changing core behaviour.
 *
 * <p>
 * Methods (dispatched in {@code BrokenPeriodDecliningBalanceLoanScheduleGenerator}):
 * </p>
 * <ul>
 * <li>{@code null} (no row / no value) — legacy behaviour: actual-days broken-period math applies to any
 * {@code SAME_AS_REPAYMENT_PERIOD} loan (pre-existing, non-configurable behaviour kept for compatibility).</li>
 * <li>{@link #STANDARD} — stock Fineract first period, explicit opt-out from broken-period math.</li>
 * <li>{@link #ACTUAL_DAYS_SURCHARGE} — Vivriti: interest on actual broken days; when the broken period exceeds 30 days
 * the excess surcharges the EMI, when shorter the EMI stays flat and the saved interest moves to principal.</li>
 * <li>{@link #ADJUSTED_DAYS_SYMMETRIC} — DMI: broken days normalized so the interest-start month counts as 30 days
 * (31-day month &rarr; &minus;1, February &rarr; +2); the first installment is EMI + dailyRate&times;(days&minus;30),
 * moving in BOTH directions (smaller than the EMI for short broken periods).</li>
 * <li>{@link #DAYS_360_LEVEL_EMI} — level EMI on 30/360 (used by Northern Arc): applies to EVERY period, not just the
 * first. EMI = plain annuity rounded UP to the rupee; interest =
 * round2(round2(balance&times;rate/100)/360)&times;days360(start, end); principal = EMI &minus; interest (so the first
 * installment equals the EMI); the last installment clears the balance.</li>
 * </ul>
 *
 * <p>
 * Add-only component following the {@code ForeclosureInterestResolver} pattern; reads via plain JDBC and returns
 * {@code null} when the datatable is absent, so tenants without the custom changelog keep stock behaviour.
 * </p>
 */
@Slf4j
@Component
@RequiredArgsConstructor
public class BrokenPeriodMethodResolver {

    public static final String STANDARD = "STANDARD";
    public static final String ACTUAL_DAYS_SURCHARGE = "ACTUAL_DAYS_SURCHARGE";
    public static final String ADJUSTED_DAYS_SYMMETRIC = "ADJUSTED_DAYS_SYMMETRIC";
    public static final String DAYS_360_LEVEL_EMI = "DAYS_360_LEVEL_EMI";

    private static final String DATATABLE = "loan_product_first_emi_config";

    private final JdbcTemplate jdbcTemplate;

    /** @return the configured broken-period method for the product, or null if no usable config row exists. */
    public String resolve(final Long loanProductId) {
        if (loanProductId == null) {
            return null;
        }
        try {
            return jdbcTemplate.query("SELECT broken_period_method FROM " + DATATABLE + " WHERE product_loan_id = ?",
                    rs -> rs.next() ? rs.getString("broken_period_method") : null, loanProductId);
        } catch (final DataAccessException e) {
            // Datatable/column not present for this tenant: fall back to legacy behaviour.
            log.debug("{} not readable for product {}: {}", DATATABLE, loanProductId, e.getMessage());
            return null;
        }
    }
}
