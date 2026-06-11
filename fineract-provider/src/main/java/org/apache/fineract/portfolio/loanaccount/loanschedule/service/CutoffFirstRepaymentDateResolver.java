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

import java.time.LocalDate;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.dao.DataAccessException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;

/**
 * Derives the first repayment ("first EMI") date for a loan from per-product config stored in the
 * {@code loan_product_first_emi_config} datatable (installment_day + cutoff_day), so callers don't
 * have to pass {@code repaymentsStartingFromDate}.
 *
 * <p>Rule (a common NBFC convention): if the disbursement day-of-month is &le; cutoff_day the first EMI
 * falls on the installment_day of the NEXT month, otherwise on the installment_day of the month after that.</p>
 *
 * <p>Returns {@code null} when the product has no config row (or the datatable is absent), in which case the
 * caller keeps Fineract's stock derivation. Add-only component; the only core touch-point is the single call
 * site in {@code LoanScheduleAssembler}.</p>
 */
@Slf4j
@Component
@RequiredArgsConstructor
public class CutoffFirstRepaymentDateResolver {

    private static final String DATATABLE = "loan_product_first_emi_config";

    private final JdbcTemplate jdbcTemplate;

    public LocalDate resolve(final Long loanProductId, final LocalDate disbursementDate) {
        if (loanProductId == null || disbursementDate == null) {
            return null;
        }
        final int[] config = readConfig(loanProductId);
        if (config == null) {
            return null;
        }
        final int installmentDay = config[0];
        final int cutoffDay = config[1];
        final int monthsToAdd = disbursementDate.getDayOfMonth() <= cutoffDay ? 1 : 2;
        final LocalDate targetMonth = disbursementDate.plusMonths(monthsToAdd);
        final int dayOfMonth = Math.min(installmentDay, targetMonth.lengthOfMonth());
        return targetMonth.withDayOfMonth(dayOfMonth);
    }

    /** @return [installmentDay, cutoffDay] or null if no usable config row exists for the product. */
    private int[] readConfig(final Long loanProductId) {
        try {
            return jdbcTemplate.query("SELECT installment_day, cutoff_day FROM " + DATATABLE + " WHERE product_loan_id = ?", rs -> {
                if (rs.next()) {
                    final Object installment = rs.getObject("installment_day");
                    final Object cutoff = rs.getObject("cutoff_day");
                    if (installment instanceof Number && cutoff instanceof Number) {
                        return new int[] { ((Number) installment).intValue(), ((Number) cutoff).intValue() };
                    }
                }
                return null;
            }, loanProductId);
        } catch (final DataAccessException e) {
            // Datatable not registered / not present for this tenant: fall back to stock derivation.
            log.debug("{} not readable for product {}: {}", DATATABLE, loanProductId, e.getMessage());
            return null;
        }
    }
}
