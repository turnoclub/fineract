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
package org.apache.fineract.portfolio.loanaccount.service;

import java.math.BigDecimal;
import java.math.MathContext;
import java.math.RoundingMode;
import java.time.LocalDate;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.apache.fineract.infrastructure.core.service.DateUtils;
import org.apache.fineract.portfolio.loanaccount.domain.Loan;
import org.apache.fineract.portfolio.loanaccount.domain.LoanRepaymentScheduleInstallment;
import org.springframework.dao.DataAccessException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;

/**
 * Resolves a per-product override for the foreclosure INTEREST amount from the
 * {@code loan_product_foreclosure_config} datatable (foreclosure_interest_method), so each loan product can use
 * a different foreclosure-interest logic without changing core behaviour.
 *
 * <p>For products configured {@code FLAT_ON_OUTSTANDING} the interest is a flat NBFC-style charge:
 * {@code principalOutstanding * annualRate% * days / daysInYear}, where {@code days} = actual calendar days,
 * inclusive, from the last fully-paid installment's due date (the date interest was last settled to) up to the
 * closure date, and {@code daysInYear} comes from the product's own {@code daysInYearType} (e.g. 360). This
 * differs from Fineract's stock schedule-based, declining-balance foreclosure interest.</p>
 *
 * <p>Returns {@code null} when the product has no config row, the method is not {@code FLAT_ON_OUTSTANDING}, or the
 * datatable is absent -- in which case the caller keeps Fineract's stock foreclosure derivation. Add-only
 * component; the only core touch-point is the single call site in {@code LoanBalanceService}.</p>
 */
@Slf4j
@Component
@RequiredArgsConstructor
public class ForeclosureInterestResolver {

    private static final String DATATABLE = "loan_product_foreclosure_config";
    private static final String FLAT_ON_OUTSTANDING = "FLAT_ON_OUTSTANDING";
    private static final MathContext MC = new MathContext(19, RoundingMode.HALF_EVEN);

    private final JdbcTemplate jdbcTemplate;

    /**
     * @return the flat foreclosure interest for the loan as of {@code closureDate}, or {@code null} to fall back to
     *         the stock (schedule-based) foreclosure interest.
     */
    public BigDecimal resolve(final Loan loan, final LocalDate closureDate) {
        if (loan == null || closureDate == null) {
            return null;
        }
        final String method = readMethod(loan.productId());
        if (!FLAT_ON_OUTSTANDING.equalsIgnoreCase(method)) {
            return null;
        }
        final BigDecimal principal = loan.getSummary() == null ? null : loan.getSummary().getTotalPrincipalOutstanding();
        final BigDecimal annualRatePercent = loan.getLoanProductRelatedDetail().getAnnualNominalInterestRate();
        if (principal == null || annualRatePercent == null) {
            return null;
        }
        final int daysInYear = loan.getLoanProductRelatedDetail().fetchDaysInYearType().getNumberOfDays(closureDate);
        if (daysInYear <= 0) {
            return null;
        }
        final LocalDate anchor = lastSettledDate(loan);
        if (anchor == null) {
            // No paid installment and no disbursement date to anchor from: keep stock derivation.
            return null;
        }
        // Inclusive day count from the last settled date to the closure date.
        final long days = DateUtils.getExactDifferenceInDays(anchor, closureDate) + 1L;
        if (days < 1L) {
            log.warn("{}: closure date {} is before last settled date {} for product {}; charging zero foreclosure interest",
                    DATATABLE, closureDate, anchor, loan.productId());
            return BigDecimal.ZERO;
        }
        // interest = principal * (annualRate / 100) * days / daysInYear. Returned unrounded; the caller wraps it
        // in Money.of(currency, ...) which rounds to the currency's scale.
        return principal.multiply(annualRatePercent, MC).multiply(BigDecimal.valueOf(days), MC)
                .divide(BigDecimal.valueOf(100L * daysInYear), MC);
    }

    /** Due date of the latest fully-paid installment (interest settled to here); disbursement date if none paid. */
    private LocalDate lastSettledDate(final Loan loan) {
        LocalDate anchor = loan.getDisbursementDate();
        for (final LoanRepaymentScheduleInstallment installment : loan.getRepaymentScheduleInstallments()) {
            final LocalDate dueDate = installment.getDueDate();
            if (installment.isObligationsMet() && dueDate != null && (anchor == null || DateUtils.isAfter(dueDate, anchor))) {
                anchor = dueDate;
            }
        }
        return anchor;
    }

    /** @return the configured method for the product, or null if no usable config row / datatable exists. */
    private String readMethod(final Long loanProductId) {
        if (loanProductId == null) {
            return null;
        }
        try {
            return jdbcTemplate.query("SELECT foreclosure_interest_method FROM " + DATATABLE + " WHERE product_loan_id = ?",
                    rs -> rs.next() ? rs.getString("foreclosure_interest_method") : null, loanProductId);
        } catch (final DataAccessException e) {
            // Datatable not registered / not present for this tenant: fall back to stock derivation.
            log.debug("{} not readable for product {}: {}", DATATABLE, loanProductId, e.getMessage());
            return null;
        }
    }
}
